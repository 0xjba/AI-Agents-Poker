from typing import List, Dict, Optional, Set, Any, Union
import logging
import asyncio
from dataclasses import dataclass
from web3 import Web3
import json
import os
from datetime import datetime
from enum import IntEnum
from eth_abi import encode
from .openrouter_client import OpenRouterClient
from .constants import BettingRound, PlayerAction

# Import transcript at module level but handle import errors gracefully
try:
    from .transcript_manager import transcript
    TRANSCRIPT_AVAILABLE = True
except ImportError:
    TRANSCRIPT_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("Could not import transcript_manager. Logging to transcript will be disabled.")

logger = logging.getLogger(__name__)

class PlayerStatus(IntEnum):
    INACTIVE = 0
    ACTIVE = 1
    FOLDED = 2
    ELIMINATED = 3
    ALL_IN = 4

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
        
        # Add variable to store the reasoning for actions
        self.action_reasoning = None
        
        # Initialize player state cache for UI display
        self._cached_player_state = PlayerState(
            stack=0,
            status=PlayerStatus.ACTIVE,
            current_bet=0,
            position=0,
            hole_cards=[0, 0],
            last_action_time=0
        )
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
            "Provide concise reasoning (max 150 chars) explaining your decision.\n\n"
            "Response format (JSON):\n"
            "{\n"
            '    "action": 0-3,\n'
            '    "amount": raise_amount,  // optional, only if action is 3\n'
            '    "reasoning": "Brief explanation (150 chars max)"\n'
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
            
            # Get reasoning if available (or use fallback)
            action_reasoning = "No reasoning provided"
            
            if hasattr(self, 'action_reasoning') and self.action_reasoning:
                action_reasoning = self.action_reasoning
            elif decision and 'reasoning' in decision:
                action_reasoning = decision.get('reasoning', "No reasoning provided")
                if len(action_reasoning) > 150:  # Enforce max length
                    action_reasoning = action_reasoning[:147] + "..."
            
            # Log the reasoning that will be used
            logger.info(f"Action reasoning: {action_reasoning}")
            
            # Encode the reasoning as bytes
            encoded_reasoning = action_reasoning.encode('utf-8')
            
            # Handle different action types
            if action_type == 0 or action_type == 1 or action_type == 2:  # FOLD, CHECK, or CALL
                # For FOLD, CHECK, and CALL - just include reasoning
                data = encoded_reasoning
                function_data = self.router.encodeABI(fn_name="routeGameAction", args=[action_type, data])
            else:  # RAISE
                # For RAISE - need to include amount and reasoning
                # Check if amount is at least current bet (important for smart contract)
                current_game_state = await self.get_game_state()
                if amount < current_game_state.current_bet:
                    logger.warning(f"Auto-adjusting raise amount: {amount} -> {current_game_state.current_bet}")
                    amount = current_game_state.current_bet
                
                # Double check requirements - contract might need at least 2x current bet
                if amount < current_game_state.current_bet * 2:
                    logger.warning(f"Further adjusting raise to 2x current bet: {amount} -> {current_game_state.current_bet * 2}")
                    amount = current_game_state.current_bet * 2
                    
                # Log to make debugging easier
                logger.info(f"Encoded RAISE amount: {amount} (current bet: {current_game_state.current_bet})")
                
                # For RAISE, encode both amount and reasoning
                # First encode the amount as a uint256 (32 bytes)
                amount_data = encode(['uint256'], [amount])
                
                # Combine amount data with reasoning bytes
                data = amount_data + encoded_reasoning
                function_data = self.router.encodeABI(fn_name="routeGameAction", args=[action_type, data])
            
            # Estimate gas instead of hardcoding
            try:
                # Estimate gas instead of hardcoding
                estimated_gas = self.web3.eth.estimate_gas({
                    'from': self.account.address, 
                    'to': self.router.address,
                    'data': function_data,
                    'value': 0
                })
                gas_limit = int(estimated_gas * 1.3)  # Add 30% buffer
                logger.info(f"Gas estimation successful: {estimated_gas} (adding 30% buffer: {gas_limit})")
            except Exception as e:
                logger.error(f"Gas estimation failed: {e}")
                # Fall back to default gas limit if estimation fails
                gas_limit = 350000  # Increased from 300000
                
            # Build transaction
            tx = {
                'from': self.account.address,
                'to': self.router.address,
                'gas': gas_limit,
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
                    
                    # Add to transaction monitoring if UI module is available
                    try:
                        from .terminal_ui import monitor_transaction, update_transaction
                        action_names = {0: "FOLD", 1: "CHECK", 2: "CALL", 3: "RAISE"}
                        action_name = action_names.get(action_type, f"ACTION({action_type})")
                        if action_type == 3:
                            action_name += f" {amount}"
                        monitor_transaction(tx_hash.hex(), action_name, "Pending", f"Player: {self.account.address[:8]}...")
                    except ImportError:
                        pass  # Terminal UI not available
                    
                    # Wait for transaction confirmation
                    receipt = self.web3.eth.wait_for_transaction_receipt(
                        tx_hash,
                        timeout=60,
                        poll_latency=2
                    )
                    
                    if receipt['status'] == 1:
                        # Success handling
                        self.current_hand_investment += investment
                        
                        # Safely get action type name with a fallback
                        action_type_names = {0: 'FOLD', 1: 'CHECK', 2: 'CALL', 3: 'RAISE'}
                        action_type_name = action_type_names.get(action_type, f"UNKNOWN({action_type})")
                        
                        # Save in internal action history
                        self.action_history.append({
                            'type': action_type_name,
                            'amount': investment,
                            'time': datetime.now().isoformat()
                        })
                        
                        # Update transaction in UI if available
                        try:
                            from .terminal_ui import update_transaction, register_game_event
                            update_transaction(tx_hash.hex(), "Success", f"Player: {self.account.address[:8]}...")
                            register_game_event("ACTION", f"Player {self.account.address[:8]}... performed {action_type_name}")
                        except ImportError:
                            pass
                        
                        # Log to transcript
                        try:
                            # Get player state for stack info
                            player_state_before = player_state  # Already fetched before action
                            player_state_after = await self.get_player_state(self.account.address)
                            
                            # Convert action type to PlayerAction enum
                            action_enum_map = {
                                0: PlayerAction.FOLD,
                                1: PlayerAction.CHECK,
                                2: PlayerAction.CALL,
                                3: PlayerAction.RAISE
                            }
                            action_enum = action_enum_map.get(action_type, action_type_name)
                            
                            # Get AI reasoning if available (currently not provided due to system prompt change)
                            reasoning = None
                            # Uncomment this when reasoning is added back to system prompt
                            # if hasattr(self, 'last_reasoning') and self.last_reasoning:
                            #     reasoning = self.last_reasoning
                            # Or uncommment this to use reasoning from decision if available
                            # if decision and 'reasoning' in decision:
                            #     reasoning = decision['reasoning']
                            
                            # Log the action to transcript
                            if TRANSCRIPT_AVAILABLE:
                                try:
                                    transcript.log_player_action(
                                        player_address=self.account.address,
                                        action=action_enum,
                                        amount=amount if action_type == 3 else investment,
                                        reasoning=reasoning,
                                        is_ai=True,
                                        stack_before=player_state_before.stack if player_state_before else None,
                                        stack_after=player_state_after.stack if player_state_after else None
                                    )
                                except Exception as e:
                                    logger.error(f"Error logging action to transcript: {e}")
                            else:
                                logger.info(f"Transcript logging disabled - action not logged")
                        except Exception as e:
                            logger.error(f"Error logging action to transcript: {e}")
                        
                        logger.info(f"Action completed: type={action_type}, investment={investment}, "
                                f"total_investment={self.current_hand_investment}, tx_hash={tx_hash.hex()}")
                        return True
                    else:
                        # Transaction failed - capture more details
                        logger.error(f"Transaction failed: tx_hash={tx_hash.hex()}")
                        
                        # Update transaction in UI if available
                        try:
                            from .terminal_ui import update_transaction
                            update_transaction(tx_hash.hex(), "Failed", "Transaction reverted")
                        except ImportError:
                            pass
                        
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
                        
                        # If raise action is failing, try simpler actions on later attempts
                        if action_type == 3 and attempt < max_attempts - 1:
                            logger.warning(f"RAISE action with amount {amount} failed - trying CALL for next attempt")
                            action_type = 2  # CALL
                            amount = 0
                            # Re-encode function data for CALL
                            function_data = self.router.encodeABI(fn_name="routeGameAction", args=[action_type, b''])
                            # Re-estimate gas for the new action
                            try:
                                estimated_gas = self.web3.eth.estimate_gas({
                                    'from': self.account.address, 
                                    'to': self.router.address,
                                    'data': function_data,
                                    'value': 0
                                })
                                gas_limit = int(estimated_gas * 1.3)
                                logger.info(f"New gas estimation for CALL: {estimated_gas} (with buffer: {gas_limit})")
                            except Exception as e:
                                logger.error(f"Gas estimation for CALL failed: {e}")
                                gas_limit = 350000
                        elif action_type == 2 and attempt == max_attempts - 1:
                            # If CALL also fails and this is the last attempt, try FOLD as last resort
                            logger.warning("CALL also failed - trying FOLD as last resort")
                            action_type = 0  # FOLD
                            amount = 0
                            # Re-encode function data for FOLD
                            function_data = self.router.encodeABI(fn_name="routeGameAction", args=[action_type, b''])
                            
                        if attempt < max_attempts - 1:
                            delay = base_delay * (2 ** attempt)
                            logger.info(f"Retrying in {delay}s (attempt {attempt+1}/{max_attempts})")
                            await asyncio.sleep(delay)
                        else:
                            logger.error("All attempts failed, giving up on action")
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
            
            # Check if we have enough values in the state array
            if len(state) < 11:
                logger.error(f"Invalid game state length: {len(state)}, expected at least 11 values")
                logger.debug(f"Game state values received: {state}")
                # Return dummy state with default values
                return GameState(
                    action_timer=0,
                    community_cards=[0, 0, 0, 0, 0],
                    current_round=BettingRound(0),
                    main_pot=0,
                    current_bet=0,
                    last_raise=0,
                    min_raise=0,
                    last_aggressor=0,
                    current_turn="0x0000000000000000000000000000000000000000",
                    hand_start_time=0,
                    last_action_amount=0
                )
            
            # Safe access to state values
            action_timer = state[0] if len(state) > 0 else 0
            community_cards = state[1] if len(state) > 1 else [0, 0, 0, 0, 0]
            current_round = state[2] if len(state) > 2 else 0
            main_pot = state[3] if len(state) > 3 else 0
            current_bet = state[4] if len(state) > 4 else 0
            last_raise = state[5] if len(state) > 5 else 0
            min_raise = state[6] if len(state) > 6 else 0
            last_aggressor = state[7] if len(state) > 7 else 0
            current_turn = state[8] if len(state) > 8 else "0x0000000000000000000000000000000000000000"
            hand_start_time = state[9] if len(state) > 9 else 0
            last_action_amount = state[10] if len(state) > 10 else 0
            
            # Safely wrap current_round in BettingRound enum
            try:
                current_round_enum = BettingRound(current_round)
            except (ValueError, TypeError):
                logger.error(f"Invalid current_round value: {current_round}")
                current_round_enum = BettingRound.PREFLOP
            
            # Ensure community_cards is a list of appropriate length
            if not isinstance(community_cards, (list, tuple)) or len(community_cards) < 5:
                logger.error(f"Invalid community cards: {community_cards}")
                community_cards = [0, 0, 0, 0, 0]
                
            return GameState(
                action_timer=action_timer,
                community_cards=community_cards,
                current_round=current_round_enum,
                main_pot=main_pot,
                current_bet=current_bet,
                last_raise=last_raise,
                min_raise=min_raise,
                last_aggressor=last_aggressor,
                current_turn=current_turn,
                hand_start_time=hand_start_time,
                last_action_amount=last_action_amount
            )
        except Exception as e:
            logger.error(f"Error getting game state: {e}")
            # Return dummy state with default values rather than raising
            return GameState(
                action_timer=0,
                community_cards=[0, 0, 0, 0, 0],
                current_round=BettingRound.PREFLOP,
                main_pot=0,
                current_bet=0,
                last_raise=0,
                min_raise=0,
                last_aggressor=0,
                current_turn="0x0000000000000000000000000000000000000000",
                hand_start_time=0,
                last_action_amount=0
            )

    async def get_player_state(self, address: str) -> PlayerState:
        """Get player state from StateStorage"""
        try:
            player = self.state_storage.functions.getPlayer(address).call()
            
            # Check if we got a valid player state array
            if not player or not isinstance(player, (list, tuple)):
                logger.error(f"Invalid player state: {player}")
                # Create dummy player state
                dummy_state = PlayerState(
                    stack=0,
                    status=PlayerStatus.ACTIVE,
                    current_bet=0,
                    position=0,
                    hole_cards=[0, 0],
                    last_action_time=0
                )
                # Cache the dummy state for UI display
                self._cached_player_state = dummy_state
                return dummy_state
                
            # Check if player array has enough elements
            if len(player) < 6:
                logger.error(f"Player state too short: {len(player)}, expected at least 6 values")
                # Create dummy player state
                dummy_state = PlayerState(
                    stack=0,
                    status=PlayerStatus.ACTIVE,
                    current_bet=0,
                    position=0,
                    hole_cards=[0, 0],
                    last_action_time=0
                )
                # Cache the dummy state for UI display
                self._cached_player_state = dummy_state
                return dummy_state
                
            # Safe access to player values
            stack = player[0] if len(player) > 0 else 0
            status_value = player[1] if len(player) > 1 else 1  # Default to ACTIVE
            current_bet = player[2] if len(player) > 2 else 0
            position = player[3] if len(player) > 3 else 0
            hole_cards = player[4] if len(player) > 4 else [0, 0]
            last_action_time = player[5] if len(player) > 5 else 0
            
            # Safely convert status to enum
            try:
                status = PlayerStatus(status_value)
            except (ValueError, TypeError):
                logger.error(f"Invalid player status value: {status_value}")
                status = PlayerStatus.ACTIVE
                
            # Ensure hole_cards is a valid list
            if not isinstance(hole_cards, (list, tuple)) or len(hole_cards) < 2:
                logger.error(f"Invalid hole cards: {hole_cards}")
                hole_cards = [0, 0]
            
            # Create player state object
            player_state = PlayerState(
                stack=stack,
                status=status,
                current_bet=current_bet,
                position=position,
                hole_cards=hole_cards,
                last_action_time=last_action_time
            )
            
            # Cache the player state for UI display
            self._cached_player_state = player_state
            
            return player_state
            
        except Exception as e:
            logger.error(f"Error getting player state: {e}")
            # Return dummy state instead of raising
            dummy_state = PlayerState(
                stack=0,
                status=PlayerStatus.ACTIVE,
                current_bet=0,
                position=0,
                hole_cards=[0, 0],
                last_action_time=0
            )
            
            # Cache the dummy state as well so UI can display something
            self._cached_player_state = dummy_state
            
            return dummy_state

    def _is_new_hand(self, game_state: GameState) -> bool:
        """Detect if this is a new hand based on hand_start_time"""
        new_hand_id = game_state.hand_start_time
        
        # Only detect new hands when:
        # 1. The hand_start_time has changed from our stored value
        # 2. The hand_start_time is greater than 0 (valid timestamp)
        # 3. The current_hand_id was previously set (not the first check)
        # 4. We're at the start of a new betting round (preflop)
        
        is_new_hand = (
            self.current_hand_id is not None and  # Not our first check
            self.current_hand_id != new_hand_id and  # Hand ID changed
            new_hand_id > 0 and  # Valid timestamp
            game_state.current_round == BettingRound.PREFLOP and  # Start of hand
            game_state.main_pot == 0  # No bets placed yet
        )
        
        if is_new_hand:
            logger.info(f"New hand detected. Previous: {self.current_hand_id}, New: {new_hand_id}")
            self.current_hand_id = new_hand_id
            
            # Register new hand event in UI if available
            try:
                from .terminal_ui import register_game_event
                register_game_event("NEW_HAND", f"Hand #{new_hand_id} started")
            except ImportError:
                pass
                
            return True
            
        # Always store the current hand ID if it's not set yet
        if self.current_hand_id is None and new_hand_id > 0:
            self.current_hand_id = new_hand_id
            logger.info(f"First hand detected: {new_hand_id}")
            
            # Register first hand event in UI if available
            try:
                from .terminal_ui import register_game_event
                register_game_event("FIRST_HAND", f"First hand #{new_hand_id} started")
            except ImportError:
                pass
                
            return True
            
        return False    

    async def handle_turn(self, game_state: GameState, player_state: PlayerState):
        """Handle player's turn using LLM for decision making"""
        try:
            if self._is_recent_action():
                logger.debug("Skipping turn - too soon after last action")
                return

            # Record the exact time when we start handling the turn
            turn_start_time = datetime.now()
            logger.info(f"Turn started at {turn_start_time.strftime('%H:%M:%S.%f')[:-3]}")
            
            # Set a flag to track if we should enforce a delay (true if decision comes within 5s)
            enforce_delay = True
            target_delay = 5.0  # Target 5 seconds from turn start to action

            # Check for blinds to track investment
            await self._check_blinds(game_state, player_state)
            
            # Get additional game information (with error handling)
            active_players = 0
            previous_actions = []
            tournament_stage = "Unknown"
            
            try:
                active_players = await self._count_active_players()
            except Exception as e:
                logger.error(f"Error getting active players: {e}")
                
            try:
                previous_actions = await self._get_previous_actions()
            except Exception as e:
                logger.error(f"Error getting previous actions: {e}")
                
            try:
                tournament_stage = await self._get_tournament_stage()
            except Exception as e:
                logger.error(f"Error getting tournament stage: {e}")
                tournament_stage = "Unknown"
            
            # Format cards for readability with defensive error handling
            hole_cards = "Unknown"
            community_cards = "Unknown"
            
            try:
                # Extra validation for hole cards
                if player_state and hasattr(player_state, 'hole_cards') and player_state.hole_cards:
                    hole_cards = self._format_cards(player_state.hole_cards)
            except Exception as e:
                logger.error(f"Error formatting hole cards: {e}")
                
            try:
                # Extra validation for community cards
                if game_state and hasattr(game_state, 'community_cards') and game_state.community_cards:
                    community_cards = self._format_cards(game_state.community_cards)
            except Exception as e:
                logger.error(f"Error formatting community cards: {e}")

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

            # Check if we're already beyond the 5s target - if so, we'll execute immediately after getting LLM response
            time_check = datetime.now()
            time_elapsed_so_far = (time_check - turn_start_time).total_seconds()
            
            if time_elapsed_so_far >= target_delay:
                enforce_delay = False
                logger.info(f"Already {time_elapsed_so_far:.2f}s into turn (exceeds {target_delay}s target) before LLM call, will execute immediately after decision")

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
                    # Get LLM response with clear error message
                    try:
                        response = self.llm.get_completion(messages)
                        logger.debug(f"Raw LLM response: {response}")
                        response = response.strip()
                    except ValueError as api_error:
                        # Check for specific API error messages
                        error_str = str(api_error).lower()
                        if "insufficient" in error_str and "credit" in error_str:
                            logger.critical("OPENROUTER API KEY OUT OF CREDITS! Please check your billing and update the API key.")
                            # Emergency fallback - check if possible, otherwise fold
                            if can_check:
                                logger.info("Using emergency CHECK action due to API key issue")
                                action_type = 1  # CHECK
                                amount = 0
                            else:
                                logger.info("Using emergency FOLD action due to API key issue")
                                action_type = 0  # FOLD
                                amount = 0
                            valid_decision = True
                            break
                        else:
                            # Re-raise for other types of API errors
                            raise
                    
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
#                        if not all(k in decision for k in ['action', 'reasoning']):
                        if not all(k in decision for k in ['action']):
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
                            
                            # Minimum raise might actually be relative to big blind or the current bet
                            # Adjust min raise calculation
                            calculated_min_raise = max(min_raise, game_state.current_bet * 2)
                            
                            # Make sure raise is at least double the current bet - contract requirement
                            if raise_amount < game_state.current_bet:
                                logger.info(f"Adjusting raise: original amount {raise_amount} less than current bet {game_state.current_bet}")
                                raise_amount = game_state.current_bet
                                decision['amount'] = raise_amount
                                
                            # Log detailed raise information for debugging
                            logger.info(f"Raise details: current_bet={game_state.current_bet}, min_raise={min_raise}, "
                                      f"calculated_min={calculated_min_raise}, raise_amount={raise_amount}, "
                                      f"total_bet={call_amount + raise_amount}")
                            
                            if raise_amount < calculated_min_raise:
                                error_message = f"Attempt {attempt}: Raise amount {raise_amount} below calculated min {calculated_min_raise}"
                                logger.info(error_message)
                                
                                # Auto-adjust the raise amount to meet minimum
                                raise_amount = calculated_min_raise
                                decision['amount'] = raise_amount
                                logger.info(f"Auto-adjusted raise amount to {raise_amount}")
                                
                            if total_amount > player_state.stack:
                                error_message = f"Attempt {attempt}: Total amount {total_amount} exceeds stack {player_state.stack}"
                                logger.info(error_message)
                                continue
                        
                        # If we get here, decision is valid
                        valid_decision = True
                        # Comment out reasoning field check - uncomment when reasoning is added back to system prompt
                        # logger.info(f"Valid decision on attempt {attempt}: {decision['reasoning']}")
                        logger.info(f"Valid decision on attempt {attempt}: Action={decision['action']}")
                        
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
                
                # Set fallback reasoning for LLM failures
                self.action_reasoning = "AI is on a tea break or probably broke!"
                
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
                
                # Store reasoning from LLM if available
                if 'reasoning' in decision:
                    self.action_reasoning = decision['reasoning']
                else:
                    # Generic reasoning if LLM didn't provide one
                    action_names = {0: "FOLD", 1: "CHECK", 2: "CALL", 3: "RAISE"}
                    action_name = action_names.get(action_type, str(action_type))
                    self.action_reasoning = f"Strategic {action_name} decision based on current game state"
            
            # If enforce_delay is true, we should wait to reach exactly 5s from turn_start_time
            # If enforce_delay is false, we've already exceeded 5s and should execute immediately
            if enforce_delay:
                # Calculate how much time has passed since the turn started
                time_elapsed = (datetime.now() - turn_start_time).total_seconds()
                
                # We don't need to check for timeout here, timer agent handles actual timeouts
                
                # If we've already spent more than 5 seconds, execute immediately
                if time_elapsed >= target_delay:
                    logger.info(f"Decision took {time_elapsed:.2f}s (> {target_delay}s target), executing immediately...")
                else:
                    # Otherwise, wait only the remaining time to reach 5 seconds total
                    remaining_wait = max(0, target_delay - time_elapsed)
                    logger.info(f"Decided on action: {action_type} with amount {amount}, waiting {remaining_wait:.2f}s more to reach {target_delay}s total delay...")
                    await asyncio.sleep(remaining_wait)
            else:
                # We already took longer than 5s before getting the LLM response
                logger.info(f"LLM decision took longer than {target_delay}s target, executing immediately...")
            
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
        """Format cards into readable strings with extra defensive validation"""
        suits = ['♥', '♦', '♣', '♠']
        ranks = ['2', '3', '4', '5', '6', '7', '8', '9', 'T', 'J', 'Q', 'K', 'A']
        
        formatted = []
        
        # Extra defensive checks
        if cards is None:
            logger.warning("Cards is None, returning empty string")
            return "?"
            
        if not isinstance(cards, (list, tuple)):
            logger.warning(f"Invalid cards format (not a list): {type(cards)}")
            return "?"
            
        # Make a safe copy to avoid modifying the original
        try:
            safe_cards = list(cards)
        except Exception as e:
            logger.error(f"Error converting cards to list: {e}")
            return "?"
            
        # Process cards with extra safety
        for card in safe_cards:
            try:
                # Skip invalid cards
                if card is None or not isinstance(card, (int, float)) or card <= 0:
                    continue
                
                # Convert to int if it's a float
                if isinstance(card, float):
                    card = int(card)
                
                # Super defensive index calculations
                suit_idx = 0
                rank_idx = 0
                
                try:
                    suit_idx = card // 13
                    # Ensure we stay in bounds
                    if suit_idx < 0 or suit_idx >= len(suits):
                        suit_idx = 0
                except Exception:
                    suit_idx = 0
                    
                try:
                    rank_idx = card % 13
                    # Ensure we stay in bounds
                    if rank_idx < 0 or rank_idx >= len(ranks):
                        rank_idx = 0
                except Exception:
                    rank_idx = 0
                
                # Now it's safe to access arrays
                suit = suits[suit_idx]
                rank = ranks[rank_idx]
                formatted.append(f"{rank}{suit}")
            except Exception as e:
                logger.error(f"Error formatting card {card}: {e}")
                continue
        
        # Return formatted string or placeholder
        return ' '.join(formatted) if formatted else "?"

    async def _count_active_players(self) -> int:
        """Count number of active players with defensive error handling"""
        try:
            tournament_state = self.state_storage.functions.getTournamentStateValues().call()
            
            # Check if we have sufficient values in the array
            if not tournament_state or not isinstance(tournament_state, (list, tuple)) or len(tournament_state) <= 7:
                logger.error(f"Invalid tournament state format or insufficient elements: {tournament_state}")
                return 2  # Default to 2 as a reasonable fallback
                
            # Get activePlayerCount with safe access
            active_count = tournament_state[7]
            
            # Validate the result
            if not isinstance(active_count, int) or active_count < 0:
                logger.warning(f"Invalid active player count: {active_count}")
                return 2  # Default to 2 as a reasonable fallback
                
            return active_count
        except Exception as e:
            logger.error(f"Error getting active player count: {e}")
            return 2  # Default to 2 as a reasonable fallback

    async def _get_previous_actions(self) -> List[str]:
        """Get previous actions in current round"""
        # This would need to be implemented based on your contract's event system
        # Placeholder implementation
        return []

    async def _get_tournament_stage(self) -> str:
        """Get current tournament stage information with defensive error handling"""
        try:
            tournament_state = self.state_storage.functions.getTournamentStateValues().call()
            
            # Check if we have sufficient values in the array
            if not tournament_state or not isinstance(tournament_state, (list, tuple)):
                logger.error(f"Invalid tournament state format: {tournament_state}")
                return "Level Unknown"
                
            # Extract values with bounds checking
            level = "Unknown"
            small_blind = 0
            big_blind = 0
            
            # Get current blind level (index 9 or 10 depending on contract)
            if len(tournament_state) > 10:
                level = tournament_state[10]
            elif len(tournament_state) > 9:
                level = tournament_state[9]
            
            # Get blinds
            if len(tournament_state) > 0:
                small_blind = tournament_state[0]
            if len(tournament_state) > 1:
                big_blind = tournament_state[1]
                
            return f"Level {level}, Blinds: {small_blind}/{big_blind}"
        except Exception as e:
            logger.error(f"Error getting tournament stage details: {e}")
            return "Level Unknown"

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
                # Always get latest game and player state
                game_state = await self.get_game_state()
                
                # Get and cache player state (for UI display)
                player_state = await self.get_player_state(self.account.address)

                # Check if it's our turn - with special handling for zero address
                if game_state.current_turn == "0x0000000000000000000000000000000000000000":
                    # Game is in a non-progressive state - check if tournament is complete
                    logger.warning("Detected zero address as current turn - waiting for state to resolve...")
                    await asyncio.sleep(3)  # Wait a bit longer than usual to let contract resolve
                    continue
                    
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

            try:
                if not game_state:
                    game_state = await self.get_game_state()
                    
                if not game_state:
                    logger.error("Failed to get game state, cannot process turn")
                    return
            except Exception as e:
                logger.error(f"Error getting game state: {e}")
                return

            try:
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
            except Exception as e:
                logger.error(f"Error in turn processing initial setup: {e}")
                return

            try:
                # Verify it's still our turn
                if game_state.current_turn.lower() != self.account.address.lower():
                    return

                player_state = await self.get_player_state(self.account.address)
                if not player_state:
                    logger.error("Failed to get player state, cannot process turn")
                    return
                    
                # Check if player is all-in - can't take actions when all-in
                if player_state.status == PlayerStatus.ALL_IN:
                    logger.info("Player is all-in, cannot take action this round")
                    # Record this turn as processed to avoid repeated processing
                    turn_id = f"{game_state.hand_start_time}-{game_state.current_round}-{game_state.current_turn}"
                    self.processed_turns.add(turn_id)
                    return
            except Exception as e:
                logger.error(f"Error checking turn validity: {e}")
                return
                
            try:    
                # Check for blinds
                await self._check_blinds(game_state, player_state)
            except Exception as e:
                logger.error(f"Error checking blinds: {e}")
                # Continue to handle turn even if blind check fails
                
            try:
                # Handle the turn
                await self.handle_turn(game_state, player_state)
                
                # Record this turn as processed
                self.processed_turns.add(turn_id)
                self.last_action_time = datetime.now()

                # Keep processed turns set from growing too large
                if len(self.processed_turns) > 1000:
                    self.processed_turns = set(list(self.processed_turns)[-500:])
            except Exception as e:
                logger.error(f"Error in turn handling: {e}")
                # Try to apply a fallback action if turn handling fails
                try:
                    logger.info("Attempting fallback action (FOLD)")
                    await self.make_action(0)  # FOLD as fallback
                except Exception as fallback_error:
                    logger.error(f"Fallback action also failed: {fallback_error}")

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
                    try:
                        # Manual decoding is more reliable than using contract event parsing
                        # Extract player address directly from the indexed parameter (topic)
                        player_address = None
                        # Add defensive checks for log structure
                        if not log or not isinstance(log, dict):
                            logger.warning(f"Invalid log format: {log}")
                            continue
                            
                        if 'topics' not in log or not log['topics']:
                            logger.warning(f"No topics in log: {log}")
                            continue
                            
                        if len(log['topics']) >= 2:
                            try:
                                # Get player address from topic (indexed parameter) with defensive programming
                                topic_hex = log['topics'][1].hex() if hasattr(log['topics'][1], 'hex') else str(log['topics'][1])
                                # Make sure we have enough characters before taking the last 40
                                if len(topic_hex) >= 40:
                                    player_address_hex = topic_hex[-40:]
                                    player_address = self.web3.to_checksum_address('0x' + player_address_hex)
                                else:
                                    logger.warning(f"Topic hex too short: {topic_hex}")
                                    continue
                            except Exception as inner_e:
                                logger.error(f"Error extracting player address from topic: {inner_e}")
                                continue
                                
                        if player_address and player_address.lower() == self.account.address.lower():
                            logger.info(f"Turn event detected: Block {log.get('blockNumber', 'unknown')}")
                            await self.process_turn()
                    except Exception as e:
                        logger.error(f"Error processing event log: {e}")
                        continue

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
                    # Only process turn if we're not ALL_IN
                    if player_state.status != PlayerStatus.ALL_IN:
                        logger.info(f"Detected my turn via polling: {self.account.address}")
                        await self.process_turn(game_state)
                    else:
                        logger.info("It's my turn but I'm all-in, cannot take action")
                
                # Check if player is eliminated or all-in
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
        # If player is all-in, they can't take any actions
        if player_state.status == PlayerStatus.ALL_IN:
            return {
                "fold": False,
                "check": False,
                "call": False,
                "raise": False,
                "min_raise": 0,
                "max_raise": 0
            }
            
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
        """Stop the agent and clean up all resources"""
        logger.info(f"Stopping poker agent for {self.account.address}...")
        self.is_running = False
        
        # Give time for monitoring loops to recognize stopped state
        await asyncio.sleep(0.5)
        
        # Cancel all tasks associated with this agent
        tasks = [t for t in asyncio.all_tasks() 
                if t != asyncio.current_task() and 
                f"agent-{self.account.address}" in str(t) and not t.done()]
        
        if tasks:
            logger.info(f"Cancelling {len(tasks)} agent tasks")
            for task in tasks:
                task.cancel()
            
            # Wait for tasks to be cancelled
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
                logger.info("All agent tasks cancelled successfully")
            except Exception as e:
                logger.error(f"Error cancelling agent tasks: {e}")
        
        # Close any open web3 connections
        if hasattr(self, 'web3') and self.web3:
            try:
                provider = self.web3.provider
                if hasattr(provider, 'close'):
                    await provider.close()
            except Exception as e:
                logger.error(f"Error closing Web3 provider: {e}")
        
        # Close OpenRouter client if exists
        if hasattr(self, 'openrouter_client') and self.openrouter_client:
            try:
                await self.openrouter_client.close()
            except Exception as e:
                logger.error(f"Error closing OpenRouter client: {e}")
        
        logger.info(f"Agent for {self.account.address} stopped successfully")