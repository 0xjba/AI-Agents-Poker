from typing import List, Dict, Optional
import logging
import asyncio
from dataclasses import dataclass
from web3 import Web3
import asyncio
import json
import logging
from typing import Optional, Set
from dataclasses import dataclass
from datetime import datetime
import json
import os
from datetime import datetime
from enum import IntEnum
from eth_abi import encode
from .openrouter_client import OpenRouterClient

logger = logging.getLogger(__name__)

class BettingRound(IntEnum):
    PREFLOP = 0
    FLOP = 1
    TURN = 2
    RIVER = 3

class PlayerStatus(IntEnum):
    INACTIVE = 0
    ACTIVE = 1
    FOLDED = 2
    ELIMINATED = 3

@dataclass
class GameState:
    action_timer: int
    community_cards: List[int]
    current_round: BettingRound
    main_pot: int
    current_bet: int
    last_raise: int
    min_raise: int
    last_aggressor: int
    current_turn: str
    hand_start_time: int
    last_action_amount: int

@dataclass
class PlayerState:
    stack: int
    status: PlayerStatus
    current_bet: int
    position: int
    hole_cards: List[int]
    last_action_time: int

class PokerAgent:
    def __init__(self, model_name: str = "anthropic/claude-2"):
        # Existing initialization...
        self.is_running = False
        self.processed_turns = set()
        self.last_action_time = None
        self.MIN_ACTION_INTERVAL = 2
        
        # Add tracking variables for hand investment
        self.current_hand_investment = 0
        self.current_hand_id = None
        self.action_history = []
        
        # Initialize OpenRouter client
        self.llm = OpenRouterClient(model_name=model_name)
        
        # Define the system prompt directly
        self.system_prompt = (
            "You are an expert poker player making rapid strategic decisions. "
            "Analyze the situation and immediately select the SINGLE best action: FOLD (0), CHECK (1), CALL (2), or RAISE (3). "
            "If raising, specify the raise amount. "
            "Respond **only** with a valid JSON object, no additional text or markdown. "
            "Consider:\n"
            "- Pot odds and implied odds\n"
            "- Position and table dynamics\n"
            "- Hand strength and potential\n"
            "- Stack sizes and tournament stage\n"
            "- Previous betting patterns\n"
            "- Tournament vs Cash game strategy\n\n"
            "You MUST commit to a single decisive action without ambiguity\n\n"
            "Response format (JSON):\n"
            "{\n"
            '    "action": 0-3,\n'
            '    "amount": raise_amount,  // optional, only if action is 3\n'
            '    "reasoning": "concise explanation in 25 words or less",\n'
            '    "confidence": 0-100\n'
            "}"
        )

    async def initialize(self, rpc_url: str, private_key: str, 
                        router_address: str, state_storage_address: str,
                        game_logic_address: str) -> bool:
        """Initialize the agent with Web3 and contract connections"""
        try:
            # Initialize Web3 and account
            self.web3 = Web3(Web3.HTTPProvider(rpc_url))

            self.account = self.web3.eth.account.from_key(private_key)

            # Load contract ABIs
            with open('abis/Router.json', 'r') as f:
                router_abi = json.load(f)
            with open('abis/StateStorage.json', 'r') as f:
                state_storage_abi = json.load(f)
            with open('abis/GameLogic.json', 'r') as f:
                game_logic_abi = json.load(f)

            # Initialize contracts
            self.router = self.web3.eth.contract(
                address=router_address,
                abi=router_abi
            )
            
            self.state_storage = self.web3.eth.contract(
                address=state_storage_address,
                abi=state_storage_abi
            )

            self.game_logic = self.web3.eth.contract(
                address=game_logic_address,
                abi=game_logic_abi
            )

            logger.info(f"Agent initialized with address: {self.account.address}")
            return True

        except Exception as e:
            logger.error(f"Initialization failed: {e}")
            return False

    def _load_abi(self, filename: str) -> dict:
        """Load contract ABI from file"""
        try:
            with open(f'abis/{filename}', 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load ABI {filename}: {e}")
            raise

    async def make_action(self, action_type: int, amount: int = 0) -> bool:
        """Execute poker action through Router contract"""
        try:
            # Get the current game state to calculate investment
            game_state = await self.get_game_state()
            player_state = await self.get_player_state(self.account.address)
            
            # Calculate investment based on action type
            investment = 0
            
            if action_type == 2:  # CALL
                investment = game_state.current_bet - player_state.current_bet
            elif action_type == 3:  # RAISE
                call_amount = max(0, game_state.current_bet - player_state.current_bet)
                investment = call_amount + amount
            
            # Get base fee and priority fee for EIP-1559
            latest_block = self.web3.eth.get_block('latest')
            base_fee = latest_block['baseFeePerGas']
            priority_fee = self.web3.eth.max_priority_fee

            # Calculate maxFeePerGas (cap on total gas fee)
            max_fee_per_gas = int(base_fee * 1.5) + priority_fee
            max_priority_fee_per_gas = priority_fee
            
            # For FOLD and CHECK, we can pass empty bytes
            if action_type == 0 or action_type == 1:  # FOLD or CHECK
                # Use a separate method to build the transaction data
                function_data = self.router.encodeABI(fn_name="routeGameAction", args=[action_type, b''])
            else:  # CALL or RAISE
                # For RAISE, include the amount
                if action_type == 3:
                    # For RAISE, encode the amount as bytes
                    amount_hex = hex(amount)[2:].zfill(64)  # Convert to hex and pad to 32 bytes
                    data = bytes.fromhex(amount_hex)
                    function_data = self.router.encodeABI(fn_name="routeGameAction", args=[action_type, data])
                else:
                    # For CALL, use empty bytes
                    function_data = self.router.encodeABI(fn_name="routeGameAction", args=[action_type, b''])
            
            # Build transaction
            tx = {
                'from': self.account.address,
                'to': self.router.address,
                'gas': 300000,
                'maxFeePerGas': max_fee_per_gas,
                'maxPriorityFeePerGas': max_priority_fee_per_gas,
                'chainId': self.web3.eth.chain_id,
                'data': function_data,
                'value': 0
            }
            
            # Rest of function remains the same with retry logic
            max_attempts = 3
            base_delay = 2
            
            for attempt in range(max_attempts):
                try:
                    # Verification code
                    current_game_state = await self.get_game_state()
                    current_player_state = await self.get_player_state(self.account.address)
                    
                    logger.info(f"Pre-transaction validation - Current turn: {current_game_state.current_turn}")
                    logger.info(f"My address: {self.account.address}")
                    logger.info(f"My player state: Status={current_player_state.status}, Position={current_player_state.position}")
                    logger.info(f"Game state: Round={current_game_state.current_round}, CurrentBet={current_game_state.current_bet}")
                    
                    if current_game_state.current_turn.lower() != self.account.address.lower():
                        logger.warning("No longer my turn - aborting action")
                        return False
                        
                    # Update nonce for each attempt
                    tx['nonce'] = self.web3.eth.get_transaction_count(self.account.address)
                    
                    # Log transaction details before sending
                    logger.info(f"Sending transaction: Action={action_type}, Data={function_data}")
                    logger.info(f"Transaction details: {tx}")
                    
                    # Sign and send transaction
                    signed_tx = self.web3.eth.account.sign_transaction(tx, self.account.key)
                    tx_hash = self.web3.eth.send_raw_transaction(signed_tx.rawTransaction)
                    
                    logger.info(f"Transaction sent: {tx_hash.hex()}")
                    
                    # Wait for transaction confirmation
                    receipt = self.web3.eth.wait_for_transaction_receipt(
                        tx_hash,
                        timeout=60,
                        poll_latency=2
                    )
                    
                    if receipt['status'] == 1:
                        # Success handling
                        self.current_hand_investment += investment
                        
                        self.action_history.append({
                            'type': ['FOLD', 'CHECK', 'CALL', 'RAISE'][action_type],
                            'amount': investment,
                            'time': datetime.now().isoformat()
                        })
                        
                        logger.info(f"Action completed: type={action_type}, investment={investment}, "
                                f"total_investment={self.current_hand_investment}, tx_hash={tx_hash.hex()}")
                        return True
                    else:
                        # Transaction failed - capture more details
                        logger.error(f"Transaction failed: tx_hash={tx_hash.hex()}")
                        
                        # Specific error checking
                        # 1. Check if you're authorized
                        try:
                            is_whitelisted = self.router.functions.isWhitelisted(self.account.address).call()
                            logger.info(f"Player whitelisted status: {is_whitelisted}")
                        except Exception as e:
                            logger.error(f"Failed to check whitelist status: {e}")
                        
                        # 2. Check valid actions
                        try:
                            valid_actions = await self.get_valid_actions(game_state, player_state)
                            logger.info(f"Valid actions: FOLD={valid_actions[0]}, CHECK={valid_actions[1]}, CALL={valid_actions[2]}, RAISE={valid_actions[3]}")
                        except Exception as e:
                            logger.error(f"Failed to get valid actions: {e}")
                        
                        # Try to get revert reason
                        try:
                            tx_data = self.web3.eth.get_transaction(tx_hash)
                            result = self.web3.eth.call({
                                'to': tx_data['to'],
                                'from': tx_data['from'],
                                'data': tx_data['input'],
                                'value': tx_data.get('value', 0),
                                'gas': tx_data['gas'],
                                'gasPrice': tx_data.get('gasPrice', tx_data.get('maxFeePerGas', 0))
                            }, block_identifier=receipt.blockNumber)
                            logger.error(f"Transaction call result: {result}")
                        except Exception as e:
                            logger.error(f"Failed to get revert reason: {e}")
                            
                        logger.error(f"Transaction receipt details: {receipt}")
                        
                        # Get updated game state after failure
                        try:
                            post_game_state = await self.get_game_state()
                            logger.info(f"Game state after failed tx: Turn={post_game_state.current_turn}, " 
                                    f"Round={post_game_state.current_round}, Bet={post_game_state.current_bet}")
                        except Exception as e:
                            logger.error(f"Failed to get post-tx game state: {e}")
                        
                        if attempt < max_attempts - 1:
                            delay = base_delay * (2 ** attempt)
                            logger.info(f"Retrying in {delay}s (attempt {attempt+1}/{max_attempts})")
                            await asyncio.sleep(delay)
                        else:
                            return False
                except Exception as e:
                    logger.error(f"Error in transaction attempt {attempt+1}: {e}")
                    if attempt < max_attempts - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.info(f"Retrying in {delay}s (attempt {attempt+1}/{max_attempts})")
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"All {max_attempts} attempts failed, giving up.")
                        raise

            return False

        except Exception as e:
            logger.error(f"Error making action: {e}", exc_info=True)
            return False

    async def _check_blinds(self, game_state: GameState, player_state: PlayerState):
        """Check if the player posted blinds in this hand and update investment"""
        # If this is preflop and player has a current bet but we haven't tracked investment
        if (game_state.current_round == BettingRound.PREFLOP and 
                player_state.current_bet > 0 and 
                self.current_hand_investment == 0):
            
            # This is likely a blind
            self.current_hand_investment = player_state.current_bet
            logger.info(f"Detected blind: {player_state.current_bet}")
            
            # Add to action history
            self.action_history.append({
                'type': 'BLIND',
                'amount': player_state.current_bet,
                'time': datetime.now().isoformat()
            })    

    async def get_game_state(self) -> GameState:
        """Get current game state from StateStorage"""
        try:
            state = self.state_storage.functions.getGameStateValues().call()
            return GameState(
                action_timer=state[0],
                community_cards=state[1],
                current_round=BettingRound(state[2]),
                main_pot=state[3],
                current_bet=state[4],
                last_raise=state[5],
                min_raise=state[6],
                last_aggressor=state[7],
                current_turn=state[8],
                hand_start_time=state[9],
                last_action_amount=state[10]
            )
        except Exception as e:
            logger.error(f"Error getting game state: {e}")
            raise

    async def get_player_state(self, address: str) -> PlayerState:
        """Get player state from StateStorage"""
        try:
            player = self.state_storage.functions.getPlayer(address).call()
            return PlayerState(
                stack=player[0],
                status=PlayerStatus(player[1]),
                current_bet=player[2],
                position=player[3],
                hole_cards=player[4],
                last_action_time=player[5]
            )
        except Exception as e:
            logger.error(f"Error getting player state: {e}")
            raise

    def _is_new_hand(self, game_state: GameState) -> bool:
        """Detect if this is a new hand based on hand_start_time"""
        new_hand_id = game_state.hand_start_time
        
        if self.current_hand_id != new_hand_id:
            logger.info(f"New hand detected. Previous: {self.current_hand_id}, New: {new_hand_id}")
            self.current_hand_id = new_hand_id
            return True
        return False    

    async def handle_turn(self, game_state: GameState, player_state: PlayerState):
        """Handle player's turn using LLM for decision making"""
        try:
            if self._is_recent_action():
                logger.debug("Skipping turn - too soon after last action")
                return

            # Check for blinds to track investment
            await self._check_blinds(game_state, player_state)
            
            # Get additional game information
            active_players = await self._count_active_players()
            previous_actions = await self._get_previous_actions()
            tournament_stage = await self._get_tournament_stage()
            
            # Format cards for readability
            hole_cards = self._format_cards(player_state.hole_cards)
            community_cards = self._format_cards(game_state.community_cards)

            # Format game state message with investment information
            game_state_message = (
                f"Game State:\n"
                f"Hand: {hole_cards}\n"
                f"Community Cards: {community_cards}\n"
                f"Current Bet: {game_state.current_bet}\n"
                f"Your Current Bet: {player_state.current_bet}\n"
                f"Amount to Call: {max(0, game_state.current_bet - player_state.current_bet)}\n"
                f"Your Stack: {player_state.stack}\n"
                f"Your Investment This Hand: {self.current_hand_investment}\n"
                f"Pot Size: {game_state.main_pot}\n"
                f"Position: {player_state.position}\n"
                f"Active Players: {active_players}\n"
                f"Previous Actions: {previous_actions}\n"
                f"Current Round: {game_state.current_round.name}\n"
                f"Tournament Stage: {tournament_stage}"
            )

            # Add action history if available
            if self.action_history:
                action_summary = "\n\nYour actions this hand:\n"
                for action in self.action_history:
                    action_summary += f"- {action['type']}: {action['amount']} chips\n"
                game_state_message += action_summary

            # Create the messages array for OpenRouter
            messages = [
                {
                    "role": "system",
                    "content": self.system_prompt
                },
                {
                    "role": "user",
                    "content": game_state_message
                }
            ]

            # Generate unique ID for this turn
            turn_id = self._generate_turn_id(game_state)
            
            if turn_id in self.processed_turns:
                logger.debug(f"Turn {turn_id} already processed")
                return

            # ======= ENHANCED ERROR HANDLING FOR LLM RESPONSES =======
            # Variables to track decision validity
            valid_decision = False
            decision = None
            max_attempts = 3
            attempt = 0
            error_message = None
            
            # Calculate valid actions
            can_fold = True
            can_check = (game_state.current_bet == 0 or player_state.current_bet == game_state.current_bet)
            can_call = (player_state.stack >= game_state.current_bet - player_state.current_bet)
            min_raise = game_state.current_bet * 2
            can_raise = (player_state.stack >= min_raise)
            
            while attempt < max_attempts and not valid_decision:
                attempt += 1
                try:
                    # Get LLM response
                    response = self.llm.get_completion(messages)
                    logger.debug(f"Raw LLM response: {response}")
                    response = response.strip()
                    
                    # Try to clean the response
                    if response.startswith("```json"):
                        response = response.split("```json")[1]
                    if response.endswith("```"):
                        response = response.split("```")[0]
                    
                    # Try to parse JSON
                    try:
                        decision = json.loads(response.strip())
                        logger.debug(f"Parsed decision: {decision}")
                        
                        # Validate decision has required fields
                        if not all(k in decision for k in ['action', 'reasoning']):
                            error_message = f"Attempt {attempt}: Missing required fields"
                            logger.info(error_message)
                            continue
                        
                        # Validate action type
                        action_type = decision['action']
                        if action_type not in [0, 1, 2, 3]:
                            error_message = f"Attempt {attempt}: Invalid action type {action_type}"
                            logger.info(error_message)
                            continue
                        
                        # Check if action is valid in current game state
                        action_map = {0: can_fold, 1: can_check, 2: can_call, 3: can_raise}
                        action_names = {0: "FOLD", 1: "CHECK", 2: "CALL", 3: "RAISE"}
                        
                        if not action_map[action_type]:
                            error_message = f"Attempt {attempt}: Action {action_names[action_type]} not valid in current state"
                            logger.info(error_message)
                            continue
                        
                        # For raises, validate amount
                        if action_type == 3:
                            if 'amount' not in decision:
                                error_message = f"Attempt {attempt}: Raise action missing amount"
                                logger.info(error_message)
                                continue
                                
                            raise_amount = decision['amount']
                            if not isinstance(raise_amount, (int, float)) or raise_amount <= 0:
                                error_message = f"Attempt {attempt}: Invalid raise amount {raise_amount}"
                                logger.info(error_message)
                                continue
                                
                            # Convert to int if float
                            if isinstance(raise_amount, float):
                                raise_amount = int(raise_amount)
                                decision['amount'] = raise_amount
                                
                            # Check min raise and stack constraints
                            call_amount = game_state.current_bet - player_state.current_bet
                            total_amount = call_amount + raise_amount
                            
                            if raise_amount < min_raise:
                                error_message = f"Attempt {attempt}: Raise amount {raise_amount} below minimum {min_raise}"
                                logger.info(error_message)
                                continue
                                
                            if total_amount > player_state.stack:
                                error_message = f"Attempt {attempt}: Total amount {total_amount} exceeds stack {player_state.stack}"
                                logger.info(error_message)
                                continue
                        
                        # If we get here, decision is valid
                        valid_decision = True
                        logger.info(f"Valid decision on attempt {attempt}: {decision['reasoning']}")
                        
                    except json.JSONDecodeError:
                        error_message = f"Attempt {attempt}: Invalid JSON response"
                        logger.info(error_message)
                        continue
                        
                except Exception as e:
                    error_message = f"Attempt {attempt}: Unexpected error: {e}"
                    logger.error(error_message)
                    continue
                
                # Small delay between attempts
                if not valid_decision and attempt < max_attempts:
                    await asyncio.sleep(1)
            
            # ======= INTELLIGENT FALLBACK STRATEGY =======
            # If we couldn't get a valid decision after max attempts, use a strategic fallback
            # If we couldn't get a valid decision after max attempts, use a simple fallback
            if not valid_decision:
                logger.warning(f"Failed to get valid decision after {max_attempts} attempts.")
                
                if can_check:
                    # If we can check, always check
                    logger.info("Fallback: Using CHECK")
                    action_type = 1  # CHECK
                    amount = 0
                else:
                    # Otherwise fold
                    logger.info("Fallback: Using FOLD")
                    action_type = 0  # FOLD
                    amount = 0
            else:
                # Use the valid decision from LLM
                action_type = decision['action']
                amount = decision.get('amount', 0)
            
            # Execute the action
            success = await self.make_action(action_type, amount)
            
            if success:
                logger.info(f"Successfully executed action: {action_type} with amount {amount}")
                # Record this turn as processed
                self.processed_turns.add(turn_id)
                self.last_action_time = datetime.now()
                
                # Keep processed turns set from growing too large
                if len(self.processed_turns) > 1000:
                    self.processed_turns = set(list(self.processed_turns)[-500:])
            else:
                logger.error(f"Failed to execute action: {action_type} with amount {amount}")
                
        except Exception as e:
            logger.error(f"Error in turn handling: {e}")
            # Emergency fallback - if we can check, do that, otherwise fold
            try:
                game_state = await self.get_game_state()
                player_state = await self.get_player_state(self.account.address)
                
                if game_state.current_bet == 0 or game_state.current_bet == player_state.current_bet:
                    await self.make_action(1)  # CHECK
                else:
                    await self.make_action(0)  # FOLD
            except Exception as e2:
                logger.error(f"Emergency fallback also failed: {e2}")

    def _format_cards(self, cards: List[int]) -> str:
        """Format cards into readable strings"""
        suits = ['♥', '♦', '♣', '♠']
        ranks = ['2', '3', '4', '5', '6', '7', '8', '9', 'T', 'J', 'Q', 'K', 'A']
        
        formatted = []
        for card in cards:
            if card == 0:  # Skip empty/hidden cards
                continue
            suit = suits[card // 13]
            rank = ranks[card % 13]
            formatted.append(f"{rank}{suit}")
        
        return ' '.join(formatted)

    async def _count_active_players(self) -> int:
        """Count number of active players"""
        tournament_state = self.state_storage.functions.getTournamentStateValues().call()
        return tournament_state[7]  # activePlayerCount

    async def _get_previous_actions(self) -> List[str]:
        """Get previous actions in current round"""
        # This would need to be implemented based on your contract's event system
        # Placeholder implementation
        return []

    async def _get_tournament_stage(self) -> str:
        """Get current tournament stage information"""
        tournament_state = self.state_storage.functions.getTournamentStateValues().call()
        return f"Level {tournament_state[10]}, Blinds: {tournament_state[0]}/{tournament_state[1]}"

    def _is_valid_action(self, action_type: int, amount: int, 
                        game_state: GameState, player_state: PlayerState) -> bool:
        """Validate if an action is possible"""
        try:
            if action_type not in [0, 1, 2, 3]:
                return False
                
            if action_type == 1:  # CHECK
                return game_state.current_bet == 0 or player_state.current_bet == game_state.current_bet
                
            if action_type == 2:  # CALL
                call_amount = game_state.current_bet - player_state.current_bet
                return player_state.stack >= call_amount
                
            if action_type == 3:  # RAISE
                total_required = game_state.current_bet - player_state.current_bet + amount
                min_raise = game_state.current_bet * 2
                return (player_state.stack >= total_required and 
                       amount >= min_raise)
                
            return True  # FOLD is always valid
            
        except Exception as e:
            logger.error(f"Error in action validation: {e}")
            return False


    async def monitor_game_state(self):
        """Monitor game state continuously"""
        while self.is_running:
            try:
                game_state = await self.get_game_state()
                player_state = await self.get_player_state(self.account.address)

                # Check if it's our turn
                if game_state.current_turn.lower() == self.account.address.lower():
                    await self.process_turn(game_state)

                # Check if player is eliminated
                if player_state.status == PlayerStatus.ELIMINATED:
                    logger.info("Player eliminated - stopping agent")
                    self.is_running = False
                    break

                await asyncio.sleep(1)
                
            except Exception as e:
                logger.error(f"Error in game state monitoring: {e}")
                await asyncio.sleep(1)

    def _is_recent_action(self) -> bool:
        """Check if we've acted recently to prevent duplicate actions"""
        if not self.last_action_time:
            return False
        elapsed = (datetime.now() - self.last_action_time).total_seconds()
        return elapsed < self.MIN_ACTION_INTERVAL
    
    def _generate_turn_id(self, game_state: GameState) -> str:
        """Generate unique ID for this turn to prevent duplicate actions"""
        return f"{game_state.hand_start_time}_{game_state.current_round}_{game_state.current_bet}"

    async def process_turn(self, game_state: Optional[GameState] = None):
        """Process a turn, with duplicate action prevention"""
        try:
            if self._is_recent_action():
                logger.debug("Skipping turn - too soon after last action")
                return

            if not game_state:
                game_state = await self.get_game_state()

            # Check if this is a new hand
            if self._is_new_hand(game_state):
                logger.info(f"Resetting investment tracking for new hand.")
                self.current_hand_investment = 0
                self.action_history = []

            # Generate unique ID for this turn
            turn_id = self._generate_turn_id(game_state)
            
            if turn_id in self.processed_turns:
                logger.debug(f"Turn {turn_id} already processed")
                return

            # Verify it's still our turn
            if game_state.current_turn.lower() != self.account.address.lower():
                return

            player_state = await self.get_player_state(self.account.address)
            
            # Check for blinds
            await self._check_blinds(game_state, player_state)
            
            # Handle the turn
            await self.handle_turn(game_state, player_state)
            
            # Record this turn as processed
            self.processed_turns.add(turn_id)
            self.last_action_time = datetime.now()

            # Keep processed turns set from growing too large
            if len(self.processed_turns) > 1000:
                self.processed_turns = set(list(self.processed_turns)[-500:])

        except Exception as e:
            logger.error(f"Error processing turn: {e}")

    async def monitor_events(self):
        while self.is_running:
            try:
                # Get latest block number
                latest_block = self.web3.eth.block_number
                from_block = max(0, latest_block - 10)

                # Get event signature (ensuring 0x prefix)
                event_signature = self.web3.keccak(
                    text="ActionTimerStarted(address,uint256,uint256)"
                ).hex()
                if not event_signature.startswith('0x'):
                    event_signature = '0x' + event_signature

                # Format player address as 32-byte topic (ensuring 0x prefix)
                player_topic = '0x' + self.account.address.lower()[2:].rjust(64, '0')

                # Get logs
                logs = self.web3.eth.get_logs({
                    'address': self.game_logic.address,
                    'fromBlock': from_block,
                    'toBlock': 'latest',
                    'topics': [
                        event_signature
                    ]
                })

                # Process logs
                for log in logs:
                    data = self.web3.codec.decode_abi(['address', 'uint256', 'uint256'], bytes.fromhex(log['data'][2:]))
                    player_address = data[0]  # First parameter is the player address
                    if player_address.lower() == self.account.address.lower():
                        logger.info(f"Turn event detected: Block {log['blockNumber']}")
                        await self.process_turn()

            except Exception as e:
                logger.error(f"Error in event monitoring: {e}")
            
            await asyncio.sleep(1)

    async def monitor_game_state(self):
        """Monitor game state through polling"""
        while self.is_running:
            try:
                game_state = await self.get_game_state()
                player_state = await self.get_player_state(self.account.address)

                # Check if it's our turn - use case-insensitive comparison
                if game_state.current_turn.lower() == self.account.address.lower():
                    logger.info(f"Detected my turn via polling: {self.account.address}")
                    await self.process_turn(game_state)

                # Check if player is eliminated
                if player_state.status == PlayerStatus.ELIMINATED:
                    logger.info("Player eliminated - stopping agent")
                    self.is_running = False
                    break

                await asyncio.sleep(1)
                    
            except Exception as e:
                logger.error(f"Error in game state monitoring: {e}")
                await asyncio.sleep(1)      

    async def get_valid_actions(self, game_state, player_state):
        """Determine which actions are valid in the current state"""
        can_fold = True
        can_check = (game_state.current_bet == 0 or player_state.current_bet == game_state.current_bet)
        can_call = (player_state.stack >= game_state.current_bet - player_state.current_bet)
        
        min_raise = game_state.current_bet * 2
        can_raise = (player_state.stack >= min_raise)
        
        return [can_fold, can_check, can_call, can_raise]

    async def start(self):
        """Start the agent with both monitoring methods"""
        self.is_running = True
        logger.info(f"Starting poker agent with address {self.account.address}")
        logger.info(f"Monitoring GameLogic contract at {self.game_logic.address}")
        
        try:
            # Run both monitoring methods concurrently
            await asyncio.gather(
                self.monitor_game_state(),
                self.monitor_events()
            )
        except Exception as e:
            logger.error(f"Error in agent main loop: {e}")
            self.is_running = False

    async def stop(self):
        """Stop the agent"""
        self.is_running = False
        logger.info("Stopping poker agent...")
