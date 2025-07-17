from web3 import Web3
import time
import asyncio
import json
import logging
import random
from datetime import datetime, timedelta

# Import transcript at module level but handle import errors gracefully
try:
    from .transcript_manager import transcript
    TRANSCRIPT_AVAILABLE = True
except ImportError:
    TRANSCRIPT_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("Could not import transcript_manager. Logging to transcript will be disabled.")

logger = logging.getLogger(__name__)

class TimerAgent:
    def __init__(self):
        # Existing initialization
        self.is_running = False
        self.web3 = None
        self.account = None
        self.router = None
        self.game_logic = None
        self.active_timers = {}
        self.betting_round_monitor = False
        self.current_round = None
        self.last_round_check = datetime.now()
        self.ROUND_CHECK_INTERVAL = 2
        self.last_event_pruning = datetime.now()
        self.EVENT_PRUNING_INTERVAL = 60  # Prune old events every 60 seconds
        
        # Add persistsent tracking of processed blocks
        self.last_processed_block = 0
        # Dictionary to store event IDs with timestamps for better pruning
        self.processed_events = {}  # Format: {event_id: timestamp}

    async def initialize(self, rpc_url: str, private_key: str, 
                        router_address: str, game_logic_address: str, 
                        state_storage_address: str = None,
                        betting_contract_address: str = None) -> bool:
        """Initialize timer agent with contracts"""
        try:
            # Initialize Web3 and account
            self.web3 = Web3(Web3.HTTPProvider(rpc_url))
            self.account = self.web3.eth.account.from_key(private_key)
            
            # Store betting contract address
            self.betting_contract_address = betting_contract_address
            
            # Load contract ABIs
            with open('abis/Router.json', 'r') as f:
                router_abi = json.load(f)
            with open('abis/GameLogic.json', 'r') as f:
                game_logic_abi = json.load(f)
                
            # Initialize contracts
            self.router = self.web3.eth.contract(
                address=router_address,
                abi=router_abi
            )
            
            self.game_logic = self.web3.eth.contract(
                address=game_logic_address,
                abi=game_logic_abi
            )
            
            # Initialize StateStorage contract - CRITICAL FIX
            if state_storage_address:
                with open('abis/StateStorage.json', 'r') as f:
                    state_storage_abi = json.load(f)
                
                self.state_storage = self.web3.eth.contract(
                    address=state_storage_address,
                    abi=state_storage_abi
                )
            else:
                # Try to get state storage address from Router if not provided
                try:
                    state_storage_address = self.router.functions.getImplementation(0).call()
                    with open('abis/StateStorage.json', 'r') as f:
                        state_storage_abi = json.load(f)
                    
                    self.state_storage = self.web3.eth.contract(
                        address=state_storage_address,
                        abi=state_storage_abi
                    )
                    logger.info(f"StateStorage contract initialized from Router: {state_storage_address}")
                except Exception as e:
                    logger.error(f"Failed to initialize StateStorage: {e}")
                    return False
                    
            # Check if betting contract address was provided
            if self.betting_contract_address:
                logger.info(f"Betting contract address provided: {self.betting_contract_address}")
            else:
                logger.warning("No betting contract address provided. "
                             "Payout monitoring will attempt to find it during runtime.")

            # Check if this account is authorized
            try:
                is_authorized = self.router.functions.isAuthorizedTimer(
                    self.account.address
                ).call()
                
                if not is_authorized:
                    logger.error(f"Timer agent {self.account.address} is not authorized!")
                    return False
                    
                logger.info(f"Timer agent {self.account.address} is authorized")
            except Exception as e:
                logger.error(f"Error checking timer authorization: {e}")
                # Continue anyway, might be an issue with the function name

            logger.info(f"Timer agent initialized with address: {self.account.address}")
            return True

        except Exception as e:
            logger.error(f"Timer initialization failed: {e}")
            return False

            
    async def monitor_timers(self):
        """Monitor active timers and handle timeouts"""
        while self.is_running:
            try:
                # Log current active timers (only occasionally to avoid noise)
                if random.random() < 0.05:  # ~5% chance each loop
                    active_count = len(self.active_timers)
                    if active_count > 0:
                        players_with_timers = list(self.active_timers.keys())
                        logger.info(f"Current active timers ({active_count}): {players_with_timers[:5]}")
                        
                        # Log first few timers with their expiry
                        for idx, (player, expiry) in enumerate(list(self.active_timers.items())[:3]):
                            time_left = (expiry - datetime.now()).total_seconds()
                            logger.info(f"  Timer {idx+1}: {player} expires in {time_left:.1f}s")
                    
                    # Also periodically log full timer debug state for better visibility
                    self.debug_log_timer_state("Periodic check from monitor_timers")
                
                # Check for expired timers
                current_time = datetime.now()
                expired_players = [
                    player for player, expiry in self.active_timers.items()
                    if current_time >= expiry
                ]

                # Add validation - verify if it's still the player's turn before processing timeout
                game_state = await self.get_game_state()
                if game_state:
                    current_turn = game_state[8].lower() if game_state[8] else None
                    
                    # Remove any timers for players who are not the current turn
                    invalid_timers = [player for player in self.active_timers.keys() 
                                     if player.lower() != current_turn]
                    
                    for player in invalid_timers:
                        if player in self.active_timers:
                            logger.info(f"Removing stale timer for {player} - not current turn")
                            del self.active_timers[player]

                for player in expired_players:
                    # Double check it's still this player's turn
                    if game_state and game_state[8].lower() == player.lower():
                        logger.info(f"Timer expired for player {player}")
                        timeout_result = await self.process_timeout(player)
                    else:
                        logger.info(f"Skipping timeout for {player} - no longer their turn")
                    
                    # Remove timer regardless of success to prevent repeated timeouts
                    if player in self.active_timers:
                        del self.active_timers[player]
                        logger.info(f"Removed expired timer for player {player} after timeout processing")

                await asyncio.sleep(1)  # Check every second
            except Exception as e:
                logger.error(f"Error monitoring timers: {e}")
                await asyncio.sleep(1)

    async def monitor_betting_rounds(self):
        """Monitor betting rounds and handle transitions"""
        while self.is_running:
            try:
                current_time = datetime.now()
                # Check betting rounds periodically
                if (current_time - self.last_round_check).total_seconds() >= self.ROUND_CHECK_INTERVAL:
                    self.last_round_check = current_time
                    
                    # Get current game state
                    game_state = await self.get_game_state()
                    if not game_state:
                        await asyncio.sleep(1)
                        continue
                    
                    # Store current round for comparison
                    current_round = game_state[2]  # currentRound from gameStateValues
                    
                    # Check if round is complete
                    if await self.is_betting_round_complete(game_state):
                        # If no active betting (current_turn is zero address)
                        if game_state[8] == "0x0000000000000000000000000000000000000000":
                            # Log the round advancement attempt
                            import time
                            round_names = ["PreFlop", "Flop", "Turn", "River", "Showdown"]
                            current_round_name = round_names[current_round] if 0 <= current_round < len(round_names) else f"Unknown({current_round})"
                            logger.info(f"=============== ADVANCING FROM {current_round_name} ===============")
                            
                            try:
                                # Handle based on current round
                                if current_round == 0:  # PreFlop
                                    logger.info("Attempting to deal FLOP...")
                                    await self.deal_flop()
                                    logger.info("Successfully dealt FLOP!")
                                elif current_round == 1:  # Flop
                                    logger.info("Attempting to deal TURN...")
                                    await self.deal_turn()
                                    logger.info("Successfully dealt TURN!")
                                elif current_round == 2:  # Turn
                                    logger.info("Attempting to deal RIVER...")
                                    await self.deal_river()
                                    logger.info("Successfully dealt RIVER!")
                                    
                                # Verify the game advanced properly
                                try:
                                    # Get updated game state
                                    updated_game_state = await self.get_game_state()
                                    
                                    if updated_game_state:
                                        # Check if currentTurn is still zero address
                                        if updated_game_state[8] == "0x0000000000000000000000000000000000000000":
                                            logger.warning("FALLBACK FIX: Game still has zero address after advancement - calling nextRound directly")
                                            
                                            # Call nextRound explicitly to update the game state
                                            # This is a fallback in case the contract-level fix doesn't work
                                            try:
                                                # Log more details about the game state for debugging
                                                logger.info(f"Game state details before nextRound fallback: Round={updated_game_state[2]}, Pot={updated_game_state[3]}")
                                                
                                                # Try to get player counts for context
                                                active_count = 0
                                                allin_count = 0
                                                folded_count = 0
                                                
                                                try:
                                                    # This helps diagnose why the zero address persists
                                                    for i in range(8):  # MAX_PLAYERS
                                                        player_addr = self.state_storage.functions.getPlayerAtPosition(i).call()
                                                        if player_addr != "0x0000000000000000000000000000000000000000":
                                                            player_data = self.state_storage.functions.getPlayer(player_addr).call()
                                                            status = player_data[1]  # status field
                                                            if status == 1:  # ACTIVE
                                                                active_count += 1
                                                            elif status == 2:  # FOLDED
                                                                folded_count += 1
                                                            elif status == 4:  # ALL_IN
                                                                allin_count += 1
                                                    
                                                    logger.info(f"Player counts before fallback: Active={active_count}, AllIn={allin_count}, Folded={folded_count}")
                                                except Exception as count_err:
                                                    logger.error(f"Error counting players: {count_err}")
                                                
                                                # Call nextRound on GameLogic contract
                                                tx = self.game_logic.functions.nextRound().build_transaction({
                                                    'from': self.account.address,
                                                    'nonce': self.web3.eth.get_transaction_count(self.account.address),
                                                    'gas': 4000000,
                                                    'gasPrice': int(self.web3.eth.gas_price * 1.1)
                                                })
                                                signed_tx = self.web3.eth.account.sign_transaction(tx, self.account.key)
                                                tx_hash = self.web3.eth.send_raw_transaction(signed_tx.rawTransaction)
                                                receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                                                
                                                if receipt['status'] == 1:
                                                    logger.info("Successfully called nextRound to fix zero address issue")
                                                    # Re-check updated state
                                                    updated_game_state = await self.get_game_state()
                                                    if updated_game_state[8] != "0x0000000000000000000000000000000000000000":
                                                        logger.info(f"Fixed! Current turn is now {updated_game_state[8][:10]}...")
                                                    else:
                                                        logger.error("CRITICAL: Game still stuck with zero address after fallback fix!")
                                                        
                                                        # Add extra diagnostic and recovery attempt if still stuck
                                                        try:
                                                            # Try to get more context about why we're still stuck
                                                            logger.error("Attempting to diagnose persistent zero address issue...")
                                                            # Try to check if we're at the end of a hand
                                                            try:
                                                                hand_complete = self.game_logic.functions._shouldShowdown().call()
                                                                logger.info(f"Should showdown: {hand_complete}")
                                                            except Exception:
                                                                logger.error("Could not call _shouldShowdown")
                                                        except Exception as diag_err:
                                                            logger.error(f"Diagnostic error: {diag_err}")
                                                else:
                                                    logger.error(f"Failed to call nextRound fallback: {receipt}")
                                                    
                                            except Exception as nextround_err:
                                                logger.error(f"Error calling nextRound fallback: {nextround_err}")
                                        else:
                                            # Success - game advanced properly
                                            logger.info(f"Game successfully advanced to next stage. New turn: {updated_game_state[8][:10]}...")
                                        
                                        # Check if round advanced
                                        new_round = updated_game_state[2]
                                        if new_round == current_round:
                                            logger.warning(f"Round did not advance! Still at round {new_round}")
                                        else:
                                            logger.info(f"Round advanced from {current_round} to {new_round}")
                                except Exception as verify_err:
                                    logger.error(f"Error verifying game advancement: {verify_err}")
                                
                                # Log to transcript
                                if TRANSCRIPT_AVAILABLE:
                                    try:
                                        transcript.log_system_event(
                                            "ROUND_ADVANCEMENT", 
                                            {
                                                "from_round": current_round,
                                                "from_round_name": current_round_name,
                                                "to_round": updated_game_state[2] if 'updated_game_state' in locals() else "unknown",
                                                "success": True,
                                                "timestamp": int(time.time())
                                            }
                                        )
                                    except Exception as t_error:
                                        logger.error(f"Error logging to transcript: {t_error}")
                                else:
                                    logger.info(f"Transcript logging disabled - round advancement not logged")
                                
                            except Exception as adv_error:
                                # Log advancement failure
                                logger.error(f"FAILED to advance from {current_round_name}! Error: {adv_error}")
                                
                                # Try to log to transcript
                                if TRANSCRIPT_AVAILABLE:
                                    try:
                                        transcript.log_system_event(
                                            "ROUND_ADVANCEMENT_FAILURE", 
                                            {
                                                "from_round": current_round,
                                                "from_round_name": current_round_name,
                                                "error": str(adv_error),
                                                "timestamp": int(time.time())
                                            }
                                        )
                                    except Exception as transcript_e:
                                        logger.error(f"Error logging round advancement failure: {transcript_e}")
                                else:
                                    logger.info(f"Transcript logging disabled - round advancement failure not logged")
                                
                            logger.info("===========================================================")
                            
                
                await asyncio.sleep(0.5)
                    
            except Exception as e:
                logger.error(f"Error monitoring betting rounds: {e}")
                await asyncio.sleep(5)  # Longer sleep on error

    async def _check_for_allin_status_inconsistencies(self):
        """Check for players who have 0 stack but are not marked as ALL_IN"""
        try:
            # Get current game state
            game_state = await self.get_game_state()
            if not game_state:
                logger.error("Failed to get game state for ALL_IN consistency check")
                return
                
            # Check all players
            inconsistencies_found = 0
            logger.info("Running ALL_IN status consistency check...")
            
            for i in range(8):  # MAX_PLAYERS
                try:
                    # Get player address at position
                    player_addr = self.state_storage.functions.getPlayerAtPosition(i).call()
                    if player_addr and player_addr != "0x0000000000000000000000000000000000000000":
                        # Get player data
                        player_data = self.state_storage.functions.getPlayer(player_addr).call()
                        
                        # Extract key fields
                        player_stack = player_data[0]  # stack
                        player_status = player_data[1]  # status
                        player_position = player_data[3]  # position
                        
                        # Check for inconsistency - 0 stack but ACTIVE status (should be ALL_IN)
                        if player_stack == 0 and player_status == 1:  # ACTIVE
                            inconsistencies_found += 1
                            logger.warning(f"INCONSISTENCY DETECTED: Player {player_addr[:10]}... at position {player_position} has 0 stack but status is ACTIVE (1) - should be ALL_IN (4)")
                            
                            # Try to log to transcript
                            if TRANSCRIPT_AVAILABLE:
                                try:
                                    # Log detailed info about this inconsistency
                                    context = {
                                        "player": player_addr,
                                        "position": player_position,
                                        "stack": player_stack,
                                        "status": player_status,
                                        "current_round": game_state[2],
                                        "pot_size": game_state[3],
                                        "current_bet": game_state[4],
                                        "current_turn": game_state[8]
                                    }
                                    transcript.log_custom_event("ALL_IN_INCONSISTENCY_DETECTED", context)
                                    
                                    # Also log as an all-in event to get better structured data
                                    transcript.log_all_in_status(
                                        player_address=player_addr,
                                        stack_before=0,
                                        current_bet=player_data[2] if len(player_data) > 2 else 0,
                                        pot_amount=game_state[3]
                                    )
                                except Exception as e:
                                    logger.error(f"Failed to log ALL_IN inconsistency: {e}")
                            else:
                                logger.info(f"Transcript logging disabled - ALL_IN inconsistency for {player_addr[:10]}... not logged")
                except Exception as e:
                    logger.error(f"Error checking player at position {i}: {e}")
            
            # Summary log
            if inconsistencies_found > 0:
                logger.warning(f"Found {inconsistencies_found} players with 0 stack not marked as ALL_IN")
            else:
                logger.info("No ALL_IN status inconsistencies found")
                
        except Exception as e:
            logger.error(f"Error in ALL_IN consistency check: {e}")
                
    async def _prune_old_events(self):
        """Prune old events from the processed_events dictionary"""
        try:
            if not self.processed_events:
                return  # Nothing to prune
            
            import time
            current_time = time.time()
            
            # Count before pruning
            before_count = len(self.processed_events)
            
            # Define retention periods based on event type
            # Normal events: 5 minutes
            # Action events: 1 minute (since these need stricter deduplication)
            # Timeout events: 2 minutes
            retention_periods = {
                "event-timer-": 300,    # 5 minutes for timer events
                "event-action-": 60,    # 1 minute for action events 
                "timeout-": 120,        # 2 minutes for timeout events
                "default": 300          # 5 minutes for any other events
            }
            
            # Track events to remove
            events_to_remove = []
            
            # Check each event and its timestamp
            for event_id, timestamp in self.processed_events.items():
                # Determine retention period based on event type
                retention_period = retention_periods["default"]
                for prefix, period in retention_periods.items():
                    if event_id.startswith(prefix):
                        retention_period = period
                        break
                
                # Check if event is older than its retention period
                if current_time - timestamp > retention_period:
                    events_to_remove.append(event_id)
            
            # Remove old events
            for event_id in events_to_remove:
                del self.processed_events[event_id]
            
            # Log pruning results
            after_count = len(self.processed_events)
            removed_count = before_count - after_count
            
            if removed_count > 0:
                logger.info(f"Pruned {removed_count} old events from memory. Remaining: {after_count}")
            
        except Exception as e:
            logger.error(f"Error pruning old events: {e}")
    
    async def get_game_state(self):
        """Get current game state from StateStorage"""
        try:
            return self.state_storage.functions.getGameStateValues().call()
        except Exception as e:
            logger.error(f"Error getting game state: {e}")
            return None

    async def is_betting_round_complete(self, game_state):
        """Check if all players have acted and betting is equalized"""
        try:
            # If there's no current turn, it means the round is complete
            is_complete = game_state[8] == "0x0000000000000000000000000000000000000000"
            
            # Enhanced logging for zero address detection
            if is_complete:
                logger.info("================== ROUND COMPLETE DETECTED ==================")
                logger.info(f"Game state when round complete detected: {game_state}")
                logger.info(f"Current round: {game_state[2]}")  # currentRound
                logger.info(f"Main pot: {game_state[3]}")  # mainPot
                logger.info(f"Current bet: {game_state[4]}")  # currentBet
                logger.info(f"Current turn (zero address): {game_state[8]}")  # currentTurn
                
                # Get active player count for context
                try:
                    tournament_state = self.state_storage.functions.getTournamentStateValues().call()
                    logger.info(f"Active players: {tournament_state[6]}")  # activePlayerCount
                except Exception as ts_error:
                    logger.error(f"Error getting tournament state: {ts_error}")
                
                # Get all players and their statuses
                try:
                    player_statuses = []
                    for i in range(8):  # MAX_PLAYERS is typically 8
                        player_address = self.state_storage.functions.getPlayerAtPosition(i).call()
                        if player_address and player_address != "0x0000000000000000000000000000000000000000":
                            player = self.state_storage.functions.getPlayer(player_address).call()
                            status = player[1]  # status value
                            status_names = ["INACTIVE", "ACTIVE", "FOLDED", "ELIMINATED", "ALL_IN"]
                            status_text = status_names[status] if 0 <= status < len(status_names) else f"UNKNOWN({status})"
                            player_statuses.append(f"Player at position {i}: {player_address[:10]}... Status: {status_text}")
                    logger.info("Player statuses:")
                    for status in player_statuses:
                        logger.info(f"  {status}")
                except Exception as ps_error:
                    logger.error(f"Error getting player statuses: {ps_error}")
                
                logger.info("============================================================")
                
                # Log to transcript if available
                try:
                    from .transcript_manager import transcript
                    transcript.log_system_event(
                        "ROUND_COMPLETE", 
                        {
                            "round": game_state[2],
                            "pot": game_state[3],
                            "current_bet": game_state[4],
                            "player_statuses": player_statuses if 'player_statuses' in locals() else [],
                            "timestamp": int(time.time())
                        }
                    )
                except Exception as t_error:
                    logger.error(f"Error logging to transcript: {t_error}")
            
            return is_complete
        except Exception as e:
            logger.error(f"Error checking if round is complete: {e}")
            return False

    async def deal_flop(self):
        """Deal the flop through the Router's dealFlopAndAdvance function"""
        try:
            logger.info("============= DEALING FLOP - TRANSACTION START =============")
            
            # Capture game state before the transaction for comparison
            before_state = await self.get_game_state()
            logger.info(f"Game state before dealing flop: Round={before_state[2]}, Turn={before_state[8]}")
            
            # Build transaction using the Router's dealFlopAndAdvance function
            try:
                current_nonce = self.web3.eth.get_transaction_count(self.account.address)
                gas_price = int(self.web3.eth.gas_price * 1.1)
                
                tx = self.router.functions.dealFlopAndAdvance().build_transaction({
                    'from': self.account.address,
                    'nonce': current_nonce,
                    'gas': 4000000,  # Increase gas limit for the combined operation
                    'gasPrice': gas_price
                })
                
                logger.info(f"Transaction built: Nonce={current_nonce}, Gas={400000}, GasPrice={gas_price}")
            except Exception as e:
                logger.error(f"Failed to build transaction: {e}")
                raise

            # Sign and send transaction with retry logic
            max_attempts = 3
            base_delay = 2  # seconds
            
            for attempt in range(max_attempts):
                try:
                    # Re-get nonce for each attempt to avoid nonce errors
                    if attempt > 0:
                        tx['nonce'] = self.web3.eth.get_transaction_count(self.account.address)
                        logger.info(f"Updated nonce for retry: {tx['nonce']}")
                    
                    logger.info(f"Signing and sending transaction (attempt {attempt+1}/{max_attempts})")
                    signed_tx = self.web3.eth.account.sign_transaction(tx, self.account.key)
                    tx_hash = self.web3.eth.send_raw_transaction(signed_tx.rawTransaction)
                    logger.info(f"Transaction sent: {tx_hash.hex()}")
                    
                    logger.info(f"Waiting for receipt...")
                    receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                    
                    if receipt['status'] == 1:
                        logger.info(f"Flop dealt successfully (tx: {tx_hash.hex()})")
                        
                        # Verify game state after transaction
                        await asyncio.sleep(1)  # Brief pause to let state update
                        after_state = await self.get_game_state()
                        
                        if after_state:
                            logger.info(f"Game state after dealing flop: Round={after_state[2]}, Turn={after_state[8]}")
                            
                            # Check for successful round advancement
                            if after_state[2] > before_state[2]:
                                logger.info(f"Round successfully advanced from {before_state[2]} to {after_state[2]}")
                            else:
                                logger.warning(f"Round did not advance! Before={before_state[2]}, After={after_state[2]}")
                                
                            # Check if currentTurn was updated
                            if after_state[8] == "0x0000000000000000000000000000000000000000":
                                logger.error("CRITICAL: Current turn is still zero address after dealing flop!")
                            else:
                                logger.info(f"Current turn updated to: {after_state[8]}")
                        
                        logger.info("============= DEALING FLOP - TRANSACTION SUCCESS =============")
                        return True
                    else:
                        logger.error(f"Deal flop transaction failed (tx: {tx_hash.hex()})")
                        logger.error(f"Receipt: {receipt}")
                        
                        # Try to get revert reason
                        try:
                            tx_data = self.web3.eth.get_transaction(tx_hash)
                            self.web3.eth.call(
                                {
                                    'to': tx_data['to'],
                                    'from': tx_data['from'],
                                    'data': tx_data['input'],
                                    'gas': tx_data['gas'],
                                    'gasPrice': tx_data['gasPrice'],
                                    'value': tx_data['value']
                                },
                                tx_data['blockNumber']
                            )
                        except Exception as call_ex:
                            logger.error(f"Revert reason: {str(call_ex)}")
                        
                        if attempt < max_attempts - 1:
                            delay = base_delay * (2 ** attempt)
                            logger.info(f"Retrying in {delay}s (attempt {attempt+1}/{max_attempts})")
                            await asyncio.sleep(delay)
                        else:
                            logger.error("============= DEALING FLOP - TRANSACTION FAILED =============")
                            return False
                except Exception as e:
                    logger.error(f"Error in transaction attempt {attempt+1}: {e}")
                    if attempt < max_attempts - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.info(f"Retrying in {delay}s (attempt {attempt+1}/{max_attempts})")
                        await asyncio.sleep(delay)
                    else:
                        logger.error("============= DEALING FLOP - TRANSACTION ERROR =============")
                        logger.error(f"Failed to deal flop after {max_attempts} attempts")
                        raise
            
            return False
        except Exception as e:
            logger.error(f"Error dealing flop: {e}")
            return False

    async def deal_turn(self):
        """Deal the turn through the Router's dealTurnAndAdvance function"""
        try:
            logger.info("============= DEALING TURN - TRANSACTION START =============")
            
            # Capture game state before the transaction for comparison
            before_state = await self.get_game_state()
            logger.info(f"Game state before dealing turn: Round={before_state[2]}, Turn={before_state[8]}")
            
            # Build transaction using Router's dealTurnAndAdvance function
            tx = self.router.functions.dealTurnAndAdvance().build_transaction({
                'from': self.account.address,
                'nonce': self.web3.eth.get_transaction_count(self.account.address),
                'gas': 4000000,  # Increase gas limit for the combined operation
                'gasPrice': int(self.web3.eth.gas_price * 1.1)
            })

            # Implementation with retry logic (similar to deal_flop)
            # Sign and send transaction with retry
            max_attempts = 3
            base_delay = 2
            
            for attempt in range(max_attempts):
                try:
                    signed_tx = self.web3.eth.account.sign_transaction(tx, self.account.key)
                    tx_hash = self.web3.eth.send_raw_transaction(signed_tx.rawTransaction)
                    receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                    
                    if receipt['status'] == 1:
                        logger.info(f"Turn dealt successfully (tx: {tx_hash.hex()})")
                        return True
                    else:
                        logger.error(f"Deal turn transaction failed (tx: {tx_hash.hex()})")
                        if attempt < max_attempts - 1:
                            await asyncio.sleep(base_delay * (2 ** attempt))
                        else:
                            return False
                except Exception as e:
                    logger.error(f"Error dealing turn (attempt {attempt+1}): {e}")
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(base_delay * (2 ** attempt))
                    else:
                        return False
            
            return False

        except Exception as e:
            logger.error(f"Error dealing turn: {e}")
            return False

    async def deal_river(self):
        """Deal the river through the Router's dealRiverAndAdvance function"""
        try:
            logger.info("============= DEALING RIVER - TRANSACTION START =============")
            
            # Capture game state before the transaction for comparison
            before_state = await self.get_game_state()
            logger.info(f"Game state before dealing river: Round={before_state[2]}, Turn={before_state[8]}")
            
            # Build transaction using Router's dealRiverAndAdvance function
            tx = self.router.functions.dealRiverAndAdvance().build_transaction({
                'from': self.account.address,
                'nonce': self.web3.eth.get_transaction_count(self.account.address),
                'gas': 4000000,  # Increase gas limit for the combined operation
                'gasPrice': int(self.web3.eth.gas_price * 1.1)
            })

            # Implementation with retry logic (similar to previous methods)
            max_attempts = 3
            base_delay = 2
            
            for attempt in range(max_attempts):
                try:
                    signed_tx = self.web3.eth.account.sign_transaction(tx, self.account.key)
                    tx_hash = self.web3.eth.send_raw_transaction(signed_tx.rawTransaction)
                    receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                    
                    if receipt['status'] == 1:
                        logger.info(f"River dealt successfully (tx: {tx_hash.hex()})")
                        return True
                    else:
                        logger.error(f"Deal river transaction failed (tx: {tx_hash.hex()})")
                        if attempt < max_attempts - 1:
                            await asyncio.sleep(base_delay * (2 ** attempt))
                        else:
                            return False
                except Exception as e:
                    logger.error(f"Error dealing river (attempt {attempt+1}): {e}")
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(base_delay * (2 ** attempt))
                    else:
                        return False
            
            return False

        except Exception as e:
            logger.error(f"Error dealing river: {e}")
            return False

    async def process_timeout(self, player_address: str):
        """Process a player timeout"""
        # Check if player is still active and it's their turn before attempting timeout
        try:
            # Create unique event ID for this timeout to prevent duplicate timeouts
            import time
            import uuid
            timeout_id = f"timeout-{player_address.lower()}-{int(time.time())}"
            
            # Check if we've already processed a timeout for this player recently (within 30 seconds)
            current_time = time.time()
            recent_timeout_for_player = False
            
            for past_id, past_time in self.processed_events.items():
                # Only check recent events (within 30 seconds)
                if past_time > current_time - 30 and "timeout-" in past_id and player_address.lower() in past_id.lower():
                    recent_timeout_for_player = True
                    logger.warning(f"DUPLICATE TIMEOUT DETECTED: Player {player_address[:10]}... already timed out within last 30 seconds ({past_id})")
                    # Remove this player from active timers
                    if player_address in self.active_timers:
                        del self.active_timers[player_address]
                    return False
            
            # Get current game state
            game_state = await self.get_game_state()
            if not game_state:
                logger.error(f"Unable to get game state before timeout")
                return False
                
            # Check if it's still this player's turn
            if game_state[8].lower() != player_address.lower():
                logger.info(f"Skipping timeout - no longer {player_address}'s turn. Current turn: {game_state[8]}")
                # Remove this player from active timers
                if player_address in self.active_timers:
                    del self.active_timers[player_address]
                # Record this as processed to prevent future attempts
                self.processed_events[timeout_id] = time.time()
                return False
            
            # Check player's status and stack
            try:
                player = self.state_storage.functions.getPlayer(player_address).call()
                player_status = player[1]  # Status field
                player_position = player[3]  # Position field
                player_stack = player[0]  # Stack field
                current_bet = game_state[4]  # Current bet from game state
                
                logger.info(f"Player {player_address} - Status: {player_status}, Position: {player_position}, Stack: {player_stack}, Current Bet: {current_bet}")
                
                
                if player_status != 1:  # Not active
                    logger.info(f"Skipping timeout - player {player_address} no longer active (status={player_status})")
                    # Remove from active timers
                    if player_address in self.active_timers:
                        del self.active_timers[player_address]
                    return False
            except Exception as e:
                logger.error(f"Error checking player status: {e}")
        except Exception as e:
            logger.error(f"Error in pre-timeout validation: {e}")
        
        # CRITICAL: Before timing out, verify all player positions
        try:
            # Get tournament state to check active player count
            tournament_state = await self.get_tournament_state()
            if tournament_state and len(tournament_state) > 6:
                active_player_count = tournament_state[6]  # Active player count index
                
                # Log position information for all players for debugging
                logger.info(f"Active player count: {active_player_count}")
                logger.info("Player positions before timeout:")
                
                # Load all active players to verify positions
                active_players = []
                try:
                    # Get all players using state storage getActivePlayers()
                    active_players_addrs = self.state_storage.functions.getActivePlayers().call()
                    
                    # Get position for each player
                    for addr in active_players_addrs:
                        try:
                            player_data = self.state_storage.functions.getPlayer(addr).call()
                            position = player_data[3]  # Position field
                            status = player_data[1]  # Status field
                            active_players.append({
                                'address': addr,
                                'position': position,
                                'status': status
                            })
                            logger.info(f"  Player {addr[:8]}... Position: {position}, Status: {status}")
                        except Exception as inner_e:
                            logger.error(f"Error getting player data for {addr}: {inner_e}")
                except Exception as e:
                    logger.error(f"Error getting active players: {e}")
                    
                # Verify position integrity - check for duplicate positions
                positions = [p['position'] for p in active_players]
                position_counts = {}
                for pos in positions:
                    position_counts[pos] = position_counts.get(pos, 0) + 1
                
                # Check if any position has more than one player
                duplicate_positions = [pos for pos, count in position_counts.items() if count > 1]
                if duplicate_positions:
                    logger.warning(f"DUPLICATE POSITIONS DETECTED: {duplicate_positions}")
                    logger.warning("This may cause inconsistent behavior during timeouts")
        except Exception as e:
            logger.error(f"Error verifying player positions: {e}")
        
        # Implement retry logic similar to update_blinds
        max_attempts = 3
        base_delay = 2  # seconds
        
        for attempt in range(max_attempts):
            try:
                # Before each attempt, verify it's still this player's turn
                current_game_state = await self.get_game_state()
                if not current_game_state or current_game_state[8].lower() != player_address.lower():
                    logger.info(f"Aborting timeout - no longer {player_address}'s turn")
                    return False
                
                # Get fresh nonce and gas price for each attempt
                current_nonce = self.web3.eth.get_transaction_count(self.account.address)
                gas_price = int(self.web3.eth.gas_price * 1.2)  # Increase gas price by 20%
                
                logger.info(f"Timeout attempt {attempt+1}: Using nonce {current_nonce}, gas price {gas_price}")
                
                # Always try direct GameLogic call first - it's more reliable
                try:
                    # Call handlePlayerTimeout directly on GameLogic
                    tx = self.game_logic.functions.handlePlayerTimeout(
                        player_address
                    ).build_transaction({
                        'from': self.account.address,
                        'nonce': current_nonce,
                        'gas': 4000000,  # Increased gas limit
                        'gasPrice': gas_price
                    })
                    logger.info(f"Using direct GameLogic handlePlayerTimeout call")
                except Exception as e:
                    logger.error(f"GameLogic handlePlayerTimeout not available: {e}")
                    
                    # Only as a last resort, try router
                    if attempt == max_attempts - 1:
                        logger.info("Direct call failed, trying router as last resort")
                        tx = self.router.functions.routeTimeoutAction(
                            player_address
                        ).build_transaction({
                            'from': self.account.address,
                            'nonce': current_nonce,
                            'gas': 4000000,
                            'gasPrice': gas_price
                        })
                    else:
                        # Retry with direct call again
                        await asyncio.sleep(base_delay * (2 ** attempt))
                        continue

                # Sign and send transaction
                signed_tx = self.web3.eth.account.sign_transaction(tx, self.account.key)
                tx_hash = self.web3.eth.send_raw_transaction(signed_tx.rawTransaction)
                
                # Register transaction with UI if available
                try:
                    from .terminal_ui import monitor_transaction
                    # Get short player address for display
                    short_addr = player_address[:8] + "..." + player_address[-6:] if len(player_address) > 14 else player_address
                    monitor_transaction(tx_hash.hex(), "TIMEOUT", "Pending", f"Player: {short_addr}")
                except ImportError:
                    pass
                
                # Wait for transaction receipt
                receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                
                if receipt['status'] == 1:
                    logger.info(f"Timeout processed for player: {player_address} (tx: {tx_hash.hex()})")
                    
                    # Add this timeout to processed events to prevent duplicates
                    self.processed_events[timeout_id] = time.time()
                    logger.info(f"Recorded timeout in processed events with ID: {timeout_id}")
                    
                    # Clean up all timers after a successful timeout
                    self.active_timers.clear()
                    logger.info("Cleared all active timers after successful timeout")
                    
                    # Check player positions after timeout
                    player_positions = []
                    try:
                        logger.info("Player positions after timeout:")
                        # Try to get active players again to verify positions
                        active_players_addrs = self.state_storage.functions.getActivePlayers().call()
                        for addr in active_players_addrs:
                            try:
                                player_data = self.state_storage.functions.getPlayer(addr).call()
                                position = player_data[3]  # Position field
                                status = player_data[1]  # Status field
                                stack = player_data[0]   # Stack field
                                
                                # Add to positions list for logging
                                player_positions.append({
                                    'address': addr,
                                    'position': position,
                                    'status': status,
                                    'stack': stack
                                })
                                
                                logger.info(f"  Player {addr[:8]}... Position: {position}, Status: {status}")
                            except Exception as inner_e:
                                logger.error(f"Error getting player data for {addr}: {inner_e}")
                                
                        # Log to transcript
                        try:
                            transcript.log_timeout(player_address, True, player_positions)
                        except Exception as log_e:
                            logger.error(f"Error logging timeout to transcript: {log_e}")
                    except Exception as e:
                        logger.error(f"Error checking player positions after timeout: {e}")
                        # Still try to log the timeout even if we couldn't get positions
                        try:
                            transcript.log_timeout(player_address, True)
                        except Exception as log_e:
                            logger.error(f"Error logging basic timeout to transcript: {log_e}")
                    
                    # Register timeout event in UI if available
                    try:
                        from .terminal_ui import register_game_event, monitor_transaction, update_transaction
                        # Register timeout event
                        register_game_event("TIMEOUT", f"Player {player_address[:8]}... timed out")
                        # Update transaction status
                        update_transaction(tx_hash.hex(), "Success", f"Timeout for player {player_address[:8]}...")
                    except ImportError:
                        pass
                        
                    return True
                else:
                    logger.error(f"Timeout transaction failed for player: {player_address} (tx: {tx_hash.hex()})")
                    
                    # Try to get detailed error information
                    try:
                        # Get game state to understand context of failure
                        game_state = await self.get_game_state()
                        logger.info(f"Game state at failure - CurrentTurn: {game_state[8]}, " +
                                  f"CurrentRound: {game_state[2]}, CurrentBet: {game_state[4]}")
                        
                        # Get player positions for transcript logging
                        player_positions = []
                        active_players_addrs = []
                        try:
                            # Find active players by checking each position
                            MAX_PLAYERS = 5  # Maximum number of players from PokerConstants library
                            
                            for pos in range(MAX_PLAYERS):
                                try:
                                    # Get player address at this position
                                    player_addr = self.state_storage.functions.getPlayerAtPosition(pos).call()
                                    
                                    # Skip empty positions
                                    if player_addr == "0x0000000000000000000000000000000000000000":
                                        continue
                                        
                                    # Get player state to check status
                                    player_data = self.state_storage.functions.getPlayer(player_addr).call()
                                    player_status = player_data[1]  # Status is the second field (index 1)
                                    
                                    # If player is active, add to list (active=1)
                                    if player_status == 1:  # PlayerStatus.Active
                                        active_players_addrs.append(player_addr)
                                    
                                    # Add to player positions for all non-empty positions
                                    player_positions.append({
                                        'address': player_addr,
                                        'position': player_data[3],
                                        'status': player_data[1],
                                        'stack': player_data[0]
                                    })
                                except Exception as e:
                                    logger.debug(f"Error checking player at position {pos}: {e}")
                        except Exception as e:
                            logger.error(f"Error getting active players: {e}")
                            pass
                        
                        # Check if player is still active/valid
                        player_valid = False
                        player_status = None
                        try:
                            # Try to get the player state from StateStorage
                            player = self.state_storage.functions.getPlayer(player_address).call()
                            player_status = player[1]  # Status field index
                            player_valid = True
                            logger.info(f"Player status: {player_status} (0=Inactive, 1=Active, 2=Folded, 3=Eliminated, 4=All-In)")
                        except Exception as e:
                            logger.error(f"Failed to get player state: {e}")
                        
                        # Try to diagnose common errors
                        reason = "Unknown reason"
                        if player_valid:
                            if player_status != 1:  # If player not active
                                reason = f"Player no longer active (status={player_status})"
                                logger.info(f"Timeout likely failed because {reason}")
                                
                        # Log failed timeout to transcript with all details
                        try:
                            # Enhanced failure details for better debugging
                            failure_details = {
                                "reason": reason,
                                "current_turn": game_state[8] if game_state else "Unknown",
                                "round": game_state[2] if game_state else "Unknown",
                                "tx_hash": tx_hash.hex(),
                                "player_status": player_status,
                                "pot_size": game_state[3] if game_state and len(game_state) > 3 else "Unknown",
                                "current_bet": game_state[4] if game_state and len(game_state) > 4 else "Unknown",
                                "player_stack": player[0] if player_valid and player and len(player) > 0 else "Unknown",
                                "timer_count": len(self.active_timers),
                                "unix_time": int(time.time())
                            }
                            
                            # Regular timeout log
                            if TRANSCRIPT_AVAILABLE:
                                try:
                                    # Regular timeout log
                                    transcript.log_timeout(player_address, False, player_positions)
                                    
                                    # Enhanced detailed failure logs
                                    transcript.log_timeout_failure(
                                        player_address=player_address,
                                        reason=reason,
                                        tx_hash=tx_hash.hex(),
                                        context=failure_details
                                    )
                                    
                                    # Also keep previous format for compatibility
                                    transcript.log_custom_event("TIMEOUT FAILURE DETAILS", failure_details)
                                except Exception as transcript_e:
                                    logger.error(f"Error using transcript: {transcript_e}")
                            else:
                                logger.warning(f"Transcript logging disabled - timeout failure for {player_address[:10]}... not logged")
                        except Exception as log_e:
                            logger.error(f"Error logging timeout failure to transcript: {log_e}")
                                
                        # Check if timer agent is properly authorized
                        try:
                            is_authorized = False
                            try:
                                is_authorized = self.router.functions.isAuthorizedTimer(self.account.address).call()
                            except Exception:
                                # Maybe the function has a different name
                                # Check if we're an admin instead
                                try:
                                    is_authorized = self.router.functions.isAdmin(self.account.address).call()
                                except Exception:
                                    pass
                                
                            logger.info(f"Timer agent authorization status: {is_authorized}")
                            if not is_authorized:
                                logger.error(f"Timer agent {self.account.address} is not authorized! This is likely why timeouts are failing.")
                        except Exception as e:
                            logger.error(f"Failed to check timer authorization: {e}")
                            
                        # Try to replay the transaction to get revert reason
                        try:
                            # Get transaction data
                            tx_data = self.web3.eth.get_transaction(tx_hash)
                            # Try to call it to get revert reason
                            self.web3.eth.call({
                                'to': tx_data['to'],
                                'from': tx_data['from'],
                                'data': tx_data['input'],
                                'value': tx_data.get('value', 0),
                                'gas': tx_data['gas'],
                                'gasPrice': tx_data.get('gasPrice', tx_data.get('maxFeePerGas', 0))
                            }, block_identifier=receipt['blockNumber'])
                        except Exception as e:
                            revert_reason = str(e)
                            logger.error(f"TRANSACTION REVERT: {revert_reason}")
                            
                            # Update the reason with the actual revert message from the contract
                            # Extract the revert string from the error if possible
                            if "execution reverted" in revert_reason:
                                try:
                                    # Try to extract reason from message
                                    import re
                                    # Look for common error message patterns
                                    error_match = re.search(r"execution reverted: (.*?)('|\"|\n|$)", revert_reason)
                                    if error_match:
                                        extracted_reason = error_match.group(1)
                                        reason = f"Contract reverted: {extracted_reason}"
                                        
                                        # Log the specific revert reason prominently
                                        logger.error(f"CONTRACT REVERT REASON: {extracted_reason}")
                                        
                                        # Update the failure details with the contract revert reason
                                        failure_details["revert_reason"] = extracted_reason
                                        failure_details["reason"] = reason
                                        
                                        # Log the timeout failure with the revert reason
                                        if TRANSCRIPT_AVAILABLE:
                                            try:
                                                # First log a simple but very visible message about the revert
                                                transcript.log_custom_event("CONTRACT_REVERT", {
                                                    "player": player_address,
                                                    "revert_reason": extracted_reason,
                                                    "tx_hash": tx_hash.hex()
                                                })
                                                
                                                # Then log the detailed failure with context
                                                transcript.log_timeout_failure(
                                                    player_address=player_address,
                                                    reason=reason,  # Use the updated reason
                                                    tx_hash=tx_hash.hex(),
                                                    context=failure_details
                                                )
                                            except Exception as log_e:
                                                logger.error(f"Error logging revert reason: {log_e}")
                                except Exception as extract_err:
                                    logger.error(f"Error extracting revert reason: {extract_err}")
                    except Exception as e:
                        logger.error(f"Error getting failure details: {e}")
                    
                    if attempt < max_attempts - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.info(f"Retrying in {delay}s (attempt {attempt+1}/{max_attempts})")
                        await asyncio.sleep(delay)
                    else:
                        return False

            except Exception as e:
                # Detailed error logging
                logger.error(f"Error in timeout transaction attempt {attempt+1}: {e}")
                
                # Check if it's a nonce error and handle specifically
                error_str = str(e).lower()
                
                # Extract and log transaction failure reason
                failure_reason = str(e)
                
                # Check for common errors and provide clearer messages
                if "nonce too low" in error_str or "already known" in error_str:
                    failure_reason = "Transaction nonce issue - transaction may already be in the mempool"
                    logger.info("Nonce issue detected - will retry with updated nonce")
                    # Short delay to let the blockchain state update
                    await asyncio.sleep(1)
                elif "underpriced" in error_str:
                    failure_reason = "Transaction gas price too low - network congestion or gas spike"
                    logger.info("Transaction underpriced - will retry with higher gas price")
                    # No delay needed as we'll increase gas price on retry
                elif "execution reverted" in error_str:
                    # Extract the revert reason from the error message
                    try:
                        import re
                        error_match = re.search(r"execution reverted: (.*?)('|\"|\n|$)", error_str)
                        if error_match:
                            extracted_reason = error_match.group(1)
                            failure_reason = f"Contract reverted: {extracted_reason}"
                            logger.error(f"Contract revert reason: {extracted_reason}")
                            
                            # Log the revert reason to transcript
                            if TRANSCRIPT_AVAILABLE:
                                try:
                                    transcript.log_custom_event("CONTRACT_REVERT_DURING_SEND", {
                                        "player": player_address,
                                        "reason": extracted_reason,
                                        "attempt": attempt + 1,
                                        "unix_time": int(time.time())
                                    })
                                except Exception as t_err:
                                    logger.error(f"Error logging revert reason: {t_err}")
                    except Exception as rex_err:
                        logger.error(f"Error extracting revert reason: {rex_err}")
                else:
                    # For other errors, use exponential backoff
                    logger.error(f"Transaction failure reason: {failure_reason}")
                    if attempt < max_attempts - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.info(f"Retrying in {delay}s (attempt {attempt+1}/{max_attempts})")
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"Failed to process timeout after {max_attempts} attempts")
                        return False
        
        return False

    async def monitor_events(self):
        """Monitor contract events for turns using getLogs with persistence"""
        # Start by getting the current block and initializing from a safe point
        if self.last_processed_block == 0:
            current_block = self.web3.eth.block_number
            self.last_processed_block = max(0, current_block - 1000)  # Safe initial value
        
        while self.is_running:
            try:
                # Periodically prune old events to prevent memory leaks
                current_time = datetime.now()
                if (current_time - self.last_event_pruning).total_seconds() > self.EVENT_PRUNING_INTERVAL:
                    await self._prune_old_events()
                    self.last_event_pruning = current_time
                    
                    # Periodically check for ALL_IN players who are still marked as Active
                    await self._check_for_allin_status_inconsistencies()
                
                # Get latest block number
                latest_block = self.web3.eth.block_number
                
                # Only proceed if there are new blocks to process
                if latest_block <= self.last_processed_block:
                    await asyncio.sleep(1)
                    continue
                    
                logger.info(f"Checking blocks {self.last_processed_block+1} to {latest_block}")
                
                # First synchronize with current game state to ensure proper state
                try:
                    game_state = await self.get_game_state()
                    if game_state:
                        current_turn = game_state[8].lower() if game_state[8] else None
                        
                        # Only one player should have an active timer - the current turn
                        # Remove any invalid timers before processing new events
                        if current_turn:
                            # Keep track of players we've removed timers for
                            removed_timers = []
                            
                            for player in list(self.active_timers.keys()):
                                if player.lower() != current_turn:
                                    logger.info(f"Removing invalid timer for {player} - current turn is {current_turn}")
                                    del self.active_timers[player]
                                    removed_timers.append(player)
                            
                            # Log summary if we removed multiple timers
                            if len(removed_timers) > 1:
                                logger.warning(f"MULTIPLE TIMERS REMOVED: {removed_timers}")
                except Exception as e:
                    logger.error(f"Error synchronizing with game state: {e}")
                
                # =====================================================================
                # 1. Monitor for new action timers
                # =====================================================================
                # Get ActionTimerStarted event signature
                timer_event_signature = self.web3.keccak(
                    text="ActionTimerStarted(address,uint256,uint256)"
                ).hex()
                if not timer_event_signature.startswith('0x'):
                    timer_event_signature = '0x' + timer_event_signature

                # Get timer logs
                timer_logs = self.web3.eth.get_logs({
                    'address': self.game_logic.address,
                    'fromBlock': self.last_processed_block + 1,
                    'toBlock': latest_block,
                    'topics': [
                        timer_event_signature
                    ]
                })
                
                # Sort logs by block number, transaction index, and log index to ensure proper order
                timer_logs.sort(key=lambda x: (x['blockNumber'], x['transactionIndex'], x['logIndex']))
                
                # Process timer logs
                for log in timer_logs:
                    try:
                        # Generate unique event ID with standardized format
                        event_id = f"event-timer-{log['blockNumber']}-{log['transactionIndex']}-{log['logIndex']}"
                        
                        # Skip if we've already processed this event - CRITICAL to prevent duplicate processing
                        if event_id in self.processed_events:
                            logger.info(f"DUPLICATE PREVENTION: Skipping already processed timer event: {event_id}")
                            continue
                            
                        # Add extra safety check: ensure we don't create duplicate timers for the same player in quick succession
                        player_address = self.web3.to_checksum_address('0x' + log['topics'][1].hex()[-40:])
                        
                        # Check recent timer events for this player within a small window (30 seconds)
                        import time
                        current_time = time.time()
                        similar_event_found = False
                        cutoff_time = current_time - 30  # Past 30 seconds
                        
                        # Find any recent events for this player
                        for existing_id, timestamp in self.processed_events.items():
                            if (timestamp > cutoff_time and 
                                existing_id.startswith("event-timer-") and 
                                player_address.lower() in existing_id.lower()):
                                similar_event_found = True
                                logger.warning(f"DUPLICATE TIMER PREVENTION: Found recent timer for {player_address[:8]}... in last 30s")
                                break
                                
                        # Only proceed if no similar events were found recently
                        if similar_event_found:
                            logger.warning(f"SKIPPING POTENTIALLY DUPLICATE TIMER for {player_address[:8]}...")
                            # Still mark this event as processed to prevent reprocessing
                            self.processed_events[event_id] = time.time()
                            continue
                            
                        # Extract player address from the indexed parameter
                        player_address = self.web3.to_checksum_address('0x' + log['topics'][1].hex()[-40:])
                        
                        # Manual decoding approach - more reliable for all event types
                        data = log['data']
                        if isinstance(data, str) and data.startswith('0x'):
                            data = data[2:]  # Remove 0x prefix
                            
                        # For duration, we extract the first 32 bytes from data (uint256)
                        try:
                            if isinstance(data, str) and len(data) >= 64:  # String format needs at least 64 hex chars
                                duration_hex = data[:64]
                                duration = int(duration_hex, 16)
                            elif isinstance(data, bytes) and len(data) >= 32:  # Bytes format needs at least 32 bytes
                                # First 32 bytes for first parameter
                                duration = int.from_bytes(data[:32], byteorder='big')
                            else:
                                # Use a reasonable default if data is malformed
                                duration = 300  # Default to 5 minutes
                                logger.warning(f"Malformed event data format, using default duration: {duration}s")
                        except (ValueError, TypeError):
                            # Fallback to a reasonable default if conversion fails
                            duration = 300  # Default to 5 minutes
                            logger.warning(f"Failed to decode duration, using default: {duration}s")
                        
                        
                        # CRITICAL RACE CONDITION FIX: Synchronize with current game state to validate this timer
                        try:
                            # Get current game state to verify this is actually the current turn player
                            current_game_state = await self.get_game_state()
                            if current_game_state and current_game_state[8]:
                                current_turn = current_game_state[8].lower()
                                
                                # Skip if this timer is for a player who isn't the current turn
                                if current_turn != player_address.lower():
                                    logger.warning(f"INVALID TIMER DETECTED: Timer event for {player_address[:8]}... " 
                                                 f"but current turn is {current_turn[:8]}... - SKIPPING")
                                    
                                    # Mark this event as processed to prevent reprocessing
                                    self.processed_events[event_id] = time.time()
                                    continue
                        except Exception as e:
                            logger.error(f"Error validating timer with game state: {e}")
                        
                        # Before setting a new timer, clear ANY existing timers
                        # Only one player should have an active timer at any time
                        if self.active_timers:
                            # Log all timers that we're clearing
                            logger.info(f"Clearing {len(self.active_timers)} existing timers before adding new timer for {player_address[:8]}...")
                            
                            # Log details of first few timers
                            for i, (p, expiry) in enumerate(list(self.active_timers.items())[:3]):
                                time_left = (expiry - datetime.now()).total_seconds()
                                logger.info(f"  {i+1}. Timer for {p[:8]}... expiring in {time_left:.1f}s")
                            
                            # Clear all timers
                            self.active_timers.clear()
                        
                        # Calculate expiry time for the new timer
                        expiry_time = datetime.now() + timedelta(seconds=duration)
                        self.active_timers[player_address] = expiry_time
                        
                        logger.info(f"Set timer for player {player_address[:8]}... - expires at {expiry_time.strftime('%H:%M:%S')} in {duration}s")
                        
                        # Mark this event as processed with current timestamp
                        import time
                        self.processed_events[event_id] = time.time()
                        
                        logger.info(f"Started timer for player {player_address}, expires at {expiry_time} (duration: {duration}s)")
                        
                        # Enhanced contract event logging for timer start
                        try:
                            # Extract transaction hash for reference
                            tx_hash = log.get('transactionHash', b'').hex() if hasattr(log.get('transactionHash', b''), 'hex') else None
                            
                            # Get additional data like block timestamp for more context
                            block_timestamp = 0
                            try:
                                block_timestamp = self.web3.eth.get_block(log['blockNumber']).timestamp
                            except Exception:
                                pass
                                
                            # Prepare detailed event data
                            event_data = {
                                "player": player_address,
                                "duration": duration,
                                "expiry_time": expiry_time.strftime("%Y-%m-%d %H:%M:%S"),
                                "block": log['blockNumber'],
                                "block_timestamp": block_timestamp
                            }
                            
                            # Try to get player position and status information
                            try:
                                player_data = self.state_storage.functions.getPlayer(player_address).call()
                                event_data["player_position"] = player_data[3]
                                event_data["player_status"] = player_data[1]
                                event_data["player_stack"] = player_data[0]
                            except Exception:
                                pass
                                
                            # Try to get game state information
                            try:
                                game_state = self.state_storage.functions.getGameStateValues().call()
                                event_data["current_round"] = game_state[2]
                                event_data["current_bet"] = game_state[4]
                                event_data["pot_size"] = game_state[3]
                            except Exception:
                                pass
                                
                            # Use our new enhanced contract event logging
                            transcript.log_contract_event("ActionTimerStarted", event_data, tx_hash)
                        except Exception as log_e:
                            logger.error(f"Error logging timer event: {log_e}")
                    except Exception as e:
                        logger.error(f"Error processing timer log: {e}")
                
                # =====================================================================
                # 2. Monitor for player actions to close timers
                # =====================================================================
                # Get ActionTaken event signature
                action_event_signature = self.web3.keccak(
                    text="ActionTaken(address,uint8,uint256)"
                ).hex()
                if not action_event_signature.startswith('0x'):
                    action_event_signature = '0x' + action_event_signature
                
                # Get action logs
                action_logs = self.web3.eth.get_logs({
                    'address': self.game_logic.address,
                    'fromBlock': self.last_processed_block + 1,
                    'toBlock': latest_block,
                    'topics': [
                        action_event_signature
                    ]
                })
                
                # Sort logs by block number, transaction index, and log index to ensure proper order
                action_logs.sort(key=lambda x: (x['blockNumber'], x['transactionIndex'], x['logIndex']))
                
                # Process action logs
                for log in action_logs:
                    try:
                        # Generate unique event ID with standardized format (same format as timer events)
                        event_id = f"event-action-{log['blockNumber']}-{log['transactionIndex']}-{log['logIndex']}"
                        
                        # Skip if we've already processed this event - CRITICAL to prevent duplicate processing
                        if event_id in self.processed_events:
                            logger.info(f"DUPLICATE PREVENTION: Skipping already processed action event: {event_id}")
                            continue
                            
                        # Extract player address from the indexed parameter
                        player_address = self.web3.to_checksum_address('0x' + log['topics'][1].hex()[-40:])
                        
                        # Extract action type from data for validation
                        action_type = None
                        try:
                            if len(data) >= 64:
                                # Second 32 bytes (after duration) should be action type
                                action_type_hex = data[64:128] if isinstance(data, str) else data[32:64]
                                if isinstance(action_type_hex, str):
                                    action_type = int(action_type_hex, 16)
                                else:
                                    action_type = int.from_bytes(action_type_hex, byteorder='big')
                            logger.info(f"Action type: {action_type} by player: {player_address[:10]}...")
                        except Exception as e:
                            logger.error(f"Error extracting action type: {e}")
                        
                        # CRITICAL FIX: Track player actions to prevent multiples in the same round
                        # Check if this player has recently taken an action (within last 5 seconds)
                        import time
                        current_time = time.time()
                        # Look through recent events to find actions by the same player
                        recent_action_by_player = False
                        for past_id, past_time in self.processed_events.items():
                            # Only check recent events (within 5 seconds)
                            if past_time > current_time - 5 and "event-action-" in past_id and player_address.lower() in past_id.lower():
                                recent_action_by_player = True
                                logger.warning(f"MULTIPLE ACTIONS DETECTED: Player {player_address[:10]}... already took action within last 5 seconds (event {past_id})")
                                break
                        
                        if recent_action_by_player:
                            logger.warning(f"BLOCKING DUPLICATE ACTION: Player {player_address[:10]}... is attempting multiple actions")
                            # Still mark this event as processed to prevent reprocessing
                            self.processed_events[event_id] = time.time()
                            continue
                            
                        # Extra safety check: avoid processing multiple action events for the same player
                        # within a small time window (15 seconds)
                        current_time = time.time()
                        similar_event_found = False
                        cutoff_time = current_time - 15  # Past 15 seconds
                        
                        for existing_id, timestamp in self.processed_events.items():
                            if (timestamp > cutoff_time and 
                                existing_id.startswith("event-action-") and 
                                player_address.lower() in existing_id.lower()):
                                similar_event_found = True
                                logger.warning(f"DUPLICATE ACTION PREVENTION: Found recent action for {player_address[:8]}... in last 15s")
                                break
                        
                        # Skip processing if we found a similar recent event
                        if similar_event_found:
                            logger.warning(f"SKIPPING POTENTIALLY DUPLICATE ACTION for {player_address[:8]}...")
                            # Still mark as processed to prevent reprocessing
                            self.processed_events[event_id] = time.time()
                            continue
                            
                        # Extract player address from the indexed parameter
                        player_address = self.web3.to_checksum_address('0x' + log['topics'][1].hex()[-40:])
                        
                        # CRITICAL: Immediately clear any pending transactions for this player
                        # This is the first action to take BEFORE any processing to prevent further timeouts
                        self.active_timers.pop(player_address, None)  # Remove player's timer if it exists
                        
                        # IMPROVED ACTION TRACKING: Extract action type to log for better debugging
                        action_type = int.from_bytes(bytes.fromhex(log['topics'][2].hex()[-2:]), byteorder='big') if len(log['topics']) > 2 else None
                        action_name = "UNKNOWN"
                        is_all_in = False
                        
                        if action_type is not None:
                            if action_type == 0:
                                action_name = "FOLD"
                            elif action_type == 1:
                                action_name = "CHECK"
                            elif action_type == 2:
                                action_name = "CALL"
                                # Check if this might be an all-in call
                                try:
                                    # Get player state to check if they went all-in
                                    player_data = self.state_storage.functions.getPlayer(player_address).call()
                                    
                                    # Consider a player ALL_IN if their status is ALL_IN (4) 
                                    # OR if they're ACTIVE (1) but have 0 stack
                                    if player_data[1] == 4 or (player_data[1] == 1 and player_data[0] == 0):
                                        action_name = "CALL (ALL-IN)"
                                        is_all_in = True
                                        
                                        # Log the status mismatch if they have 0 stack but status is not ALL_IN
                                        if player_data[1] == 1 and player_data[0] == 0:
                                            logger.warning(f"Status inconsistency: Player {player_address[:10]}... has 0 stack but status is ACTIVE (1), should be ALL_IN (4)")
                                            
                                            # Try to log to transcript
                                            if TRANSCRIPT_AVAILABLE:
                                                try:
                                                    # Get current game state for context
                                                    game_state = await self.get_game_state()
                                                    
                                                    # Log detailed info about this inconsistency
                                                    context = {
                                                        "player": player_address,
                                                        "stack": player_data[0],
                                                        "status": player_data[1],
                                                        "action": action_name,
                                                        "current_round": game_state[2] if game_state else "Unknown",
                                                        "pot_size": game_state[3] if game_state and len(game_state) > 3 else "Unknown",
                                                        "current_bet": game_state[4] if game_state and len(game_state) > 4 else "Unknown"
                                                    }
                                                    transcript.log_custom_event("CALL_ALL_IN_INCONSISTENCY", context)
                                                    
                                                    # Also log as an all-in event for better tracking
                                                    transcript.log_all_in_status(
                                                        player_address=player_address,
                                                        stack_before=0,
                                                        current_bet=player_data[2] if len(player_data) > 2 else 0,
                                                        pot_amount=game_state[3] if game_state and len(game_state) > 3 else 0
                                                    )
                                                except Exception as e:
                                                    logger.error(f"Failed to log ALL_IN inconsistency: {e}")
                                            else:
                                                logger.info(f"Transcript logging disabled - ALL_IN inconsistency for player {player_address[:10]}... not logged")
                                except Exception:
                                    pass
                            elif action_type == 3:
                                action_name = "RAISE"
                                # Check if this might be an all-in raise
                                try:
                                    # Get player state to check if they went all-in
                                    player_data = self.state_storage.functions.getPlayer(player_address).call()
                                    
                                    # Consider a player ALL_IN if their status is ALL_IN (4) 
                                    # OR if they're ACTIVE (1) but have 0 stack
                                    if player_data[1] == 4 or (player_data[1] == 1 and player_data[0] == 0):
                                        action_name = "RAISE (ALL-IN)"  
                                        is_all_in = True
                                        
                                        # Log the status mismatch if they have 0 stack but status is not ALL_IN
                                        if player_data[1] == 1 and player_data[0] == 0:
                                            logger.warning(f"Status inconsistency: Player {player_address[:10]}... has 0 stack but status is ACTIVE (1), should be ALL_IN (4)")
                                            
                                            # Try to log to transcript
                                            if TRANSCRIPT_AVAILABLE:
                                                try:
                                                    # Get current game state for context
                                                    game_state = await self.get_game_state()
                                                    
                                                    # Log detailed info about this inconsistency
                                                    context = {
                                                        "player": player_address,
                                                        "stack": player_data[0],
                                                        "status": player_data[1],
                                                        "action": action_name,
                                                        "current_round": game_state[2] if game_state else "Unknown",
                                                        "pot_size": game_state[3] if game_state and len(game_state) > 3 else "Unknown",
                                                        "current_bet": game_state[4] if game_state and len(game_state) > 4 else "Unknown"
                                                    }
                                                    transcript.log_custom_event("RAISE_ALL_IN_INCONSISTENCY", context)
                                                    
                                                    # Also log as an all-in event for better tracking
                                                    transcript.log_all_in_status(
                                                        player_address=player_address,
                                                        stack_before=0,
                                                        current_bet=player_data[2] if len(player_data) > 2 else 0,
                                                        pot_amount=game_state[3] if game_state and len(game_state) > 3 else 0
                                                    )
                                                except Exception as e:
                                                    logger.error(f"Failed to log ALL_IN inconsistency: {e}")
                                            else:
                                                logger.info(f"Transcript logging disabled - ALL_IN inconsistency for {player_address[:10]}... not logged")
                                except Exception:
                                    pass
                        
                        # Log the action (now guaranteed to have removed the timer first)
                        logger.info(f"Player {player_address} has acted ({action_name}), timer removed")
                        
                        # Log to transcript using enhanced contract event logging
                        try:
                            # Extract transaction hash for reference
                            tx_hash = log.get('transactionHash', b'').hex() if hasattr(log.get('transactionHash', b''), 'hex') else None
                            
                            # Get additional data like block timestamp for more context
                            block_timestamp = 0
                            try:
                                block_timestamp = self.web3.eth.get_block(log['blockNumber']).timestamp
                            except Exception:
                                pass
                                
                            # Prepare detailed event data
                            event_data = {
                                "player": player_address,
                                "action": action_name,
                                "action_type": action_type,
                                "block": log['blockNumber'],
                                "block_timestamp": block_timestamp,
                                "tx_index": log['transactionIndex'],
                                "is_all_in": is_all_in
                            }
                            
                            # Try to get player stack information
                            try:
                                player_data = self.state_storage.functions.getPlayer(player_address).call()
                                event_data["player_stack"] = player_data[0]
                                event_data["player_status"] = player_data[1]
                            except Exception:
                                pass
                                
                            # Use our new enhanced contract event logging
                            transcript.log_contract_event("ActionTaken", event_data, tx_hash)
                            
                            # Get current game state for enhanced logging after action
                            try:
                                # Get detailed game state
                                current_game_state = await self.get_game_state()
                                if current_game_state:
                                    # Get round name for better readability
                                    round_names = ["PreFlop", "Flop", "Turn", "River", "Showdown"]
                                    round_num = current_game_state[2]
                                    round_name = round_names[round_num] if 0 <= round_num < len(round_names) else f"Unknown({round_num})"
                                    
                                    # Log comprehensive game state after each action
                                    transcript.log_game_state(
                                        round_type=round_name,
                                        round_number=round_num,
                                        current_turn=current_game_state[8],
                                        pot_size=current_game_state[3],
                                        current_bet=current_game_state[4],
                                        community_cards=current_game_state[1]
                                    )
                                    
                                    # If player is all-in, also log the all-in status
                                    if is_all_in:
                                        transcript.log_all_in_status(
                                            player_address=player_address,
                                            stack_before=event_data.get("player_stack", 0),
                                            current_bet=current_game_state[4],
                                            pot_amount=current_game_state[3]
                                        )
                            except Exception as state_log_err:
                                logger.error(f"Error logging game state after action: {state_log_err}")
                        except Exception as log_e:
                            logger.error(f"Error logging action event: {log_e}")
                        
                        # IMPORTANT: CLEAR ANY OTHER TIMERS TO AVOID DUPLICATE TIMEOUTS
                        # This is a defensive measure against multiple timers
                        all_timers_count = len(self.active_timers)
                        if all_timers_count > 0:
                            logger.warning(f"Found {all_timers_count} other timers after player action - clearing all")
                            self.active_timers.clear()
                            try:
                                transcript.log_custom_event("ALL TIMERS CLEARED AFTER ACTION", {
                                    "action_by": player_address,
                                    "action_type": action_name,
                                    "timer_count_before": all_timers_count,
                                    "is_all_in": is_all_in
                                })
                            except Exception as log_e:
                                logger.error(f"Error logging timer clear: {log_e}")
                        
                        # IMPORTANT: If this was an ALL-IN action, we need to force a game state check
                        # to ensure betting continues properly
                        if is_all_in:
                            logger.warning(f"ALL-IN ACTION DETECTED - Player {player_address[:8]}... went all-in")
                            # Force an immediate game state check
                            try:
                                # Get fresh game state
                                fresh_game_state = await self.get_game_state()
                                if fresh_game_state:
                                    current_turn = fresh_game_state[8].lower() if fresh_game_state[8] else None
                                    
                                    # Create a timer for the next player if needed
                                    if current_turn and current_turn != "0x0000000000000000000000000000000000000000".lower():
                                        # Check that this player isn't the one who just went all-in
                                        if current_turn.lower() != player_address.lower():
                                            try:
                                                # Convert to checksum address
                                                next_player = self.web3.to_checksum_address(current_turn)
                                                # Check if the player is active
                                                player_data = self.state_storage.functions.getPlayer(next_player).call()
                                                if player_data[1] == 1:  # 1 = Active status
                                                    # Create timer for next player
                                                    duration = 40  # seconds
                                                    expiry_time = datetime.now() + timedelta(seconds=duration)
                                                    self.active_timers[next_player] = expiry_time
                                                    logger.warning(f"Created timer for next player after ALL-IN: {next_player[:8]}...")
                                                    
                                                    # Log this special case
                                                    try:
                                                        transcript.log_custom_event("NEXT PLAYER TIMER AFTER ALL-IN", {
                                                            "all_in_player": player_address,
                                                            "next_player": next_player,
                                                            "timeout_added": duration
                                                        })
                                                    except Exception as log_e:
                                                        logger.error(f"Error logging all-in next player: {log_e}")
                                            except Exception as e:
                                                logger.error(f"Error creating timer for next player after all-in: {e}")
                            except Exception as e:
                                logger.error(f"Error checking game state after all-in: {e}")
                        
                        # Mark this event as processed with current timestamp
                        import time
                        self.processed_events[event_id] = time.time()
                        
                    except Exception as e:
                        logger.error(f"Error processing action log: {e}")
                
                # =====================================================================
                # 3. Check for and clean up any stale timers + fix "betting is open" issues
                # =====================================================================
                # Verify against current game state again after processing events
                try:
                    game_state = await self.get_game_state()
                    if game_state:
                        current_turn = game_state[8].lower() if game_state[8] else None
                        main_pot = game_state[3]  # Main pot value
                        current_round = game_state[2]  # Current betting round
                        community_cards = game_state[1]  # Community cards
                        
                        # SPECIAL DETECTION FOR END OF HAND / SPLIT POT SITUATIONS:
                        # After a pot is awarded, we may have issues with game state - detect and fix this
                        # Detecting if we're in a post-showdown state by checking if:
                        # - pot is zero (pot was just distributed)
                        # - there are community cards
                        # - current turn is either 0x0 or points to a player
                        post_pot_distribution = (
                            main_pot == 0 and
                            any(card > 0 for card in community_cards if card > 0) and
                            (current_turn == "0x0000000000000000000000000000000000000000".lower() or
                             current_turn is not None)
                        )
                        
                        if post_pot_distribution:
                            logger.warning("DETECTED POST-POT DISTRIBUTION STATE - CLEARING ALL TIMERS")
                            old_timer_count = len(self.active_timers)
                            self.active_timers.clear()
                            
                            # Log this event to the transcript
                            try:
                                transcript.log_custom_event("POST-POT DISTRIBUTION CLEANUP", {
                                    "cleared_timer_count": old_timer_count,
                                    "current_turn": current_turn,
                                    "current_round": current_round,
                                    "has_community_cards": any(card > 0 for card in community_cards if card > 0)
                                })
                            except Exception as e:
                                logger.error(f"Error logging post-pot cleanup: {e}")
                                
                        # Clean up any stale timers that shouldn't be active
                        stale_timers = []
                        # Fix "betting is open" issue - check if there's a current turn but no timer
                        if current_turn and current_turn != "0x0000000000000000000000000000000000000000".lower():
                            has_active_timer = False
                            for player in self.active_timers:
                                if player.lower() == current_turn:
                                    has_active_timer = True
                                    break
                                    
                            # If no timer exists for current turn player, create one - but with additional checks
                            if not has_active_timer:
                                # Get the player to verify they're active
                                try:
                                    # Convert to checksum address before calling
                                    checksum_address = self.web3.to_checksum_address(current_turn)
                                    player = self.state_storage.functions.getPlayer(checksum_address).call()
                                    player_status = player[1]  # Status field
                                    
                                    # Only add timer if player is active and we haven't recently added a timer for this player
                                    if player_status == 1:  # Active
                                        # IMPORTANT FIX: Verify this actually IS the current turn by double-checking game state
                                        fresh_game_state = None
                                        try:
                                            # Get fresh game state to verify current_turn is still valid
                                            fresh_game_state = self.state_storage.functions.getGameStateValues().call()
                                            if fresh_game_state and fresh_game_state[8].lower() != checksum_address.lower():
                                                logger.warning(f"BETTING OPEN ISSUE - MISMATCH: Game state changed during processing")
                                                logger.warning(f"Original current_turn: {checksum_address[:8]}..., Now: {fresh_game_state[8][:8]}...")
                                                # Don't create timer for stale turn
                                                continue
                                        except Exception as gs_err:
                                            logger.error(f"Error getting fresh game state: {gs_err}")
                                            # If we can't verify, don't create a timer
                                            continue
                                        
                                        # Check if we've recently created a timer for this player
                                        import time
                                        current_time = time.time()
                                        recently_created = False
                                        
                                        # Search for recent timer events for this player in processed_events
                                        cutoff_time = current_time - 120  # Within the last 2 minutes
                                        for event_key, event_time in list(self.processed_events.items()):
                                            if (event_key.startswith("event-timer-") and 
                                                event_time > cutoff_time and 
                                                f"{checksum_address.lower()}" in event_key):
                                                # Found a recent timer for this player
                                                recently_created = True
                                                logger.info(f"Not creating duplicate timer - found recent timer for {checksum_address[:8]}...")
                                                break
                                        
                                        # Check if there are any active timers for ANY player - strict prevention of multiple timers
                                        if len(self.active_timers) > 0:
                                            # Log which timers exist
                                            logger.warning(f"Not creating timer for {checksum_address[:8]}... - other active timers exist:")
                                            for p, exp in self.active_timers.items():
                                                time_left = (exp - datetime.now()).total_seconds()
                                                logger.warning(f"  Existing timer: {p[:8]}... expires in {time_left:.1f}s")
                                            recently_created = True  # Prevent timer creation
                                        
                                        # Only create a new timer if we haven't recently created one
                                        if not recently_created:
                                            # Default timer duration - can adjust as needed
                                            duration = 40  # 40 seconds default
                                            expiry_time = datetime.now() + timedelta(seconds=duration)
                                            
                                            # CRITICAL FIX: First clear ALL existing timers to prevent duplicates
                                            if len(self.active_timers) > 0:
                                                old_timers = list(self.active_timers.keys())
                                                self.active_timers.clear()
                                                logger.warning(f"Cleared {len(old_timers)} existing timers before creating new one")
                                            
                                            # Use the checksum address in the active_timers dictionary
                                            self.active_timers[checksum_address] = expiry_time
                                            logger.warning(f"Added timer for {checksum_address[:8]}..., expires in {duration}s")
                                            
                                            # Create a synthetic event ID to track this manual timer creation
                                            manual_event_id = f"event-timer-manual-{int(time.time())}-{checksum_address.lower()}"
                                            self.processed_events[manual_event_id] = time.time()
                                            
                                            # Log to transcript
                                            try:
                                                transcript.log_custom_event("BETTING OPEN ISSUE FIXED", {
                                                    "player": checksum_address,
                                                    "timeout_added": duration,
                                                    "round": game_state[2],
                                                    "betting_open_fixed": True,
                                                    "manual_event_id": manual_event_id
                                                })
                                            except Exception as log_e:
                                                logger.error(f"Error logging betting open fix: {log_e}")
                                except Exception as e:
                                    logger.error(f"Error checking player for betting open issue: {e}")
                        
                        # Continue with stale timer removal
                        for player in list(self.active_timers.keys()):
                            if current_turn and player.lower() != current_turn:
                                logger.warning(f"Removing stale timer for {player} - current turn is {current_turn}")
                                stale_timers.append(player)
                                del self.active_timers[player]
                                
                        # Log multiple timers if detected (more than 1)
                        if len(self.active_timers) > 1:
                            # This is a critical issue - log extra debug info immediately
                            logger.warning(f"MULTIPLE ACTIVE TIMERS DETECTED: {len(self.active_timers)} timers found")
                            for player, expiry in self.active_timers.items():
                                time_left = (expiry - datetime.now()).total_seconds()
                                logger.warning(f"  Timer for {player}: expires in {time_left:.1f}s")
                            
                            # Log detailed state to help diagnose the issue
                            self.debug_log_timer_state("MULTIPLE TIMERS DETECTED")
                            
                            try:
                                transcript.log_multiple_timers(self.active_timers, current_turn)
                            except Exception as e:
                                logger.error(f"Error logging multiple timers: {e}")
                                
                        # Log stale timer removal if any were removed
                        if stale_timers:
                            try:
                                stale_details = {
                                    "stale_timers": ", ".join([t[:8]+"..." for t in stale_timers]),
                                    "current_turn": current_turn[:8]+"..." if current_turn else "None",
                                    "active_timer_count": len(self.active_timers)
                                }
                                transcript.log_custom_event("STALE TIMERS REMOVED", stale_details)
                            except Exception as e:
                                logger.error(f"Error logging stale timer removal: {e}")
                except Exception as e:
                    logger.error(f"Error cleaning up stale timers: {e}")
                
                # Update the last processed block
                self.last_processed_block = latest_block
                
                
                # Keep the processed events dictionary from growing too large
                # Using timestamp-based pruning to remove oldest events first
                if len(self.processed_events) > 10000:
                    logger.info(f"Pruning processed events dictionary (current size: {len(self.processed_events)})")
                    # Sort events by timestamp and keep the 5000 newest
                    sorted_events = sorted(self.processed_events.items(), key=lambda x: x[1])
                    self.processed_events = dict(sorted_events[-5000:])
                    logger.info(f"Pruned processed events to {len(self.processed_events)} entries")

            except Exception as e:
                logger.error(f"Error monitoring events: {e}")
            
            await asyncio.sleep(2)  # Check every 2 seconds

    async def monitor_blind_levels(self):
        """Monitor tournament blind levels and trigger increases when needed"""
        # Initialize tracking variables
        self.last_successful_blind_update = 0
        self.last_hand_end_time = 0
        self.hand_history = []  # Track the history of hands for time-based blind increases
        
        # Debug log directly to console and UI
        print("BLIND MONITOR: Started monitoring blind levels")
        logger.info("BLIND MONITOR: Started monitoring blind levels")
        
        # Try to force a log message to the UI
        try:
            from .terminal_ui import terminal_ui, LogLevel
            terminal_ui.add_log("BLIND MONITOR: Initialized blind tracking", LogLevel.INFO)
        except Exception as e:
            print(f"Error adding to UI: {e}")
        
        while self.is_running:
            try:
                # Check tournament state to see if it's active
                tournament_state = await self.get_tournament_state()
                if not tournament_state:
                    await asyncio.sleep(5)  # Longer sleep if can't get state
                    continue
                    
                # Unpack tournament state values
                (
                    small_blind,
                    big_blind,
                    blind_timer,
                    last_blind_update,
                    table_state,
                    button_position,
                    active_player_count,
                    start_time,
                    is_paused,
                    current_blind_level
                ) = tournament_state
                
                # Only check for blind updates if tournament is active and not paused
                if table_state == 1 and not is_paused:  # 1 = TableState.Active
                    # Get current game state
                    game_state = await self.get_game_state()
                    if not game_state:
                        logger.info("Skipping blind update - couldn't get game state")
                        await asyncio.sleep(5)
                        continue
                    
                    # Extract game state data
                    hand_start_time = game_state[9]  # handStartTime is at index 9
                    current_turn = game_state[8]     # currentTurn at index 8
                    current_round = game_state[2]    # currentRound at index 2
                    main_pot = game_state[3]         # mainPot at index 3
                    community_cards = game_state[1]  # communityCards at index 1
                    
                    # Check if hand is in progress
                    hand_in_progress = (
                        current_turn != "0x0000000000000000000000000000000000000000" or
                        main_pot > 0 or
                        any(card > 0 for card in community_cards if card > 0) or
                        current_round > 0
                    )
                    
                    # Check if we're between hands (hand has ended but next hasn't started)
                    between_hands = (
                        hand_start_time > 0 and      # A hand has started at some point
                        current_turn == "0x0000000000000000000000000000000000000000" and  # No active player
                        main_pot == 0 and            # No active pot
                        not any(card > 0 for card in community_cards if card > 0) and  # No community cards
                        current_round == 0           # Back to preflop (or hand not started)
                    )
                    
                    # Get current blockchain time for consistency
                    try:
                        latest_block = self.web3.eth.get_block('latest')
                        current_time = latest_block.timestamp
                    except Exception as e:
                        logger.error(f"Error getting latest block timestamp: {e}")
                        current_time = int(datetime.now().timestamp())
                    
                    # Detect hand state transitions for better blind update timing
                    if not hasattr(self, 'previous_hand_in_progress'):
                        self.previous_hand_in_progress = hand_in_progress
                    
                    # Detect hand completion (was in progress, now it's not)
                    hand_just_completed = self.previous_hand_in_progress and not hand_in_progress and between_hands
                    if hand_just_completed:
                        logger.info(f"Hand completion detected at time {current_time}")
                        self.last_hand_end_time = current_time
                        
                        # Add to hand history
                        if hasattr(self, 'last_hand_start_time'):
                            hand_duration = current_time - self.last_hand_start_time
                            self.hand_history.append({
                                'start_time': self.last_hand_start_time,
                                'end_time': current_time,
                                'duration': hand_duration
                            })
                            # Trim history to keep only last 10 hands
                            if len(self.hand_history) > 10:
                                self.hand_history = self.hand_history[-10:]
                            
                            # Log hand timing statistics
                            avg_duration = sum(h['duration'] for h in self.hand_history) / len(self.hand_history)
                            logger.info(f"Last hand duration: {hand_duration}s, Average: {avg_duration:.1f}s")
                    
                    # Detect new hand starting
                    new_hand_starting = (not self.previous_hand_in_progress and hand_in_progress and 
                                        hand_start_time > 0 and hand_start_time != getattr(self, 'last_hand_start_time', 0))
                    if new_hand_starting:
                        logger.info(f"New hand starting at time {hand_start_time}")
                        self.last_hand_start_time = hand_start_time
                        
                        # THIS IS THE KEY POINT: Update blinds when a new hand is starting,
                        # based on time elapsed since tournament start or last update
                        # Only if we're in a reasonable state to do so
                        
                        # Special case: Skip the very first hand
                        if not hasattr(self, 'first_hand_tracked') or not self.first_hand_tracked:
                            logger.info("First hand detected - setting initial reference point")
                            self.first_hand_tracked = True
                            self.first_hand_start_time = hand_start_time
                        else:
                            # We have a proper hand transition - check if blinds need updating
                            # Calculate time since the tournament started
                            tournament_elapsed_time = current_time - start_time
                            
                            # Calculate expected blind level based on elapsed time
                            expected_blind_level = tournament_elapsed_time // blind_timer
                            
                            # DEBUGGING: Force check blind levels for every hand - remove in production
                            debug_blinds = True
                            
                            # Print directly to console for debugging
                            print(f"BLIND CHECK: Current level: {current_blind_level}, Expected: {expected_blind_level}")
                            print(f"BLIND CHECK: Time elapsed: {tournament_elapsed_time}s, Timer: {blind_timer}s")
                            
                            # Try to send to UI
                            try:
                                from .terminal_ui import terminal_ui, LogLevel
                                terminal_ui.add_log(f"BLIND CHECK: Current: {current_blind_level}, Expected: {expected_blind_level}", LogLevel.INFO)
                            except Exception as e:
                                print(f"UI Log error: {e}")
                            
                            # Check if we need to update blinds
                            if expected_blind_level > current_blind_level or debug_blinds:
                                # Log through both channels
                                print(f"BLIND UPDATE NEEDED: Current: {current_blind_level}, Expected: {expected_blind_level}")
                                logger.info(f"New hand starting. Time to update blinds to match elapsed time.")
                                logger.info(f"Current level: {current_blind_level}, Expected level: {expected_blind_level}")
                                logger.info(f"Tournament elapsed time: {tournament_elapsed_time}s, Blind timer: {blind_timer}s")
                                
                                # Attempt blind update
                                success = await self.update_blinds()
                                
                                # If successful, update our tracking
                                if success:
                                    self.last_successful_blind_update = current_time
                                    
                                    # Log the new blinds
                                    try:
                                        new_tournament_state = await self.get_tournament_state()
                                        if new_tournament_state:
                                            new_small_blind = new_tournament_state[0]
                                            new_big_blind = new_tournament_state[1]
                                            new_level = new_tournament_state[9]
                                            logger.info(f"Blinds updated successfully to level {new_level}: "
                                                      f"SB={new_small_blind}, BB={new_big_blind}")
                                    except Exception as e:
                                        logger.error(f"Error getting updated tournament state: {e}")
                    
                    # Update tracking of hand state
                    self.previous_hand_in_progress = hand_in_progress
                
                # Check frequently to catch the exact moment between hands
                await asyncio.sleep(3)
                    
            except Exception as e:
                logger.error(f"Error monitoring blind levels: {e}")
                await asyncio.sleep(5)  # Longer sleep on error

    async def get_tournament_state(self):
        """Get current tournament state from StateStorage"""
        try:
            return self.state_storage.functions.getTournamentStateValues().call()
        except Exception as e:
            logger.error(f"Error getting tournament state: {e}")
            return None

    # Removed the ensure_player_all_in_status function as we'll fix the issue in the contract instead
    
    async def update_blinds(self):
        """Trigger blind level update through Router contract"""
        try:
            logger.info("Updating blind levels")
            
            # Get current blind levels for transcript
            old_blind_levels = {}
            try:
                tournament_state = await self.get_tournament_state()
                if tournament_state:
                    old_blind_levels = {
                        'small': tournament_state[0],  # smallBlind
                        'big': tournament_state[1],    # bigBlind
                        'level': tournament_state[9]   # currentBlindLevel
                    }

                if old_blind_levels['level'] > 6:
                    logger.info(f'Blind update not needed: Level {old_blind_levels["level"]} is above the limit already.')
                    return False
                
            except Exception as e:
                logger.error(f"Error getting current blind levels: {e}")
            
            # Sign and send transaction with retry logic
            max_attempts = 3
            base_delay = 2  # seconds
            
            for attempt in range(max_attempts):
                try:
                    # Get latest nonce for each attempt
                    current_nonce = self.web3.eth.get_transaction_count(self.account.address)
                    gas_price = int(self.web3.eth.gas_price * 1.2)  # Increase gas price by 20%
                    
                    logger.info(f"Attempt {attempt+1}: Using nonce {current_nonce}, gas price {gas_price}")
                    
                    # Build transaction with fresh nonce for each attempt
                    try:
                        tx = self.router.functions.routeBlindUpdate().build_transaction({
                            'from': self.account.address,
                            'nonce': current_nonce,
                            'gasPrice': gas_price
                        })
                    except Exception as e:
                        logger.error(f"Error building transaction: {e}. Fallback to 4000000 gas.")
                        tx = self.router.functions.routeBlindUpdate().build_transaction({
                            'from': self.account.address,
                            'nonce': current_nonce,
                            'gasPrice': gas_price,
                            'gas': 4000000
                        })
                    
                    signed_tx = self.web3.eth.account.sign_transaction(tx, self.account.key)
                    tx_hash = self.web3.eth.send_raw_transaction(signed_tx.rawTransaction)
                    receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                    
                    if receipt['status'] == 1:
                        logger.info(f"Blind levels updated successfully (tx: {tx_hash.hex()})")
                        
                        # Get new blind levels for comparison and transcript
                        try:
                            new_tournament_state = await self.get_tournament_state()
                            if new_tournament_state:
                                new_blind_levels = {
                                    'small': new_tournament_state[0],  # smallBlind
                                    'big': new_tournament_state[1],    # bigBlind
                                    'level': new_tournament_state[9]   # currentBlindLevel
                                }
                                
                                # Calculate elapsed time for reason
                                try:
                                    latest_block = self.web3.eth.get_block('latest')
                                    tournament_elapsed_time = latest_block.timestamp - new_tournament_state[7]  # startTime
                                    reason = f"Tournament elapsed time: {tournament_elapsed_time}s"
                                except:
                                    reason = "Tournament progression"
                                
                                # Log the blind update to transcript
                                if TRANSCRIPT_AVAILABLE:
                                    try:
                                        transcript.log_blind_increase(
                                            old_levels=old_blind_levels,
                                            new_levels=new_blind_levels,
                                            reason=reason
                                        )
                                    except Exception as transcript_e:
                                        logger.error(f"Error logging blind increase to transcript: {transcript_e}")
                                else:
                                    logger.info(f"Transcript logging disabled - blind increase not logged")
                                
                                # Enhanced debugging info logging
                                try:
                                    # Collect detailed debug information about the blind update
                                    debug_info = {
                                        "transaction": tx_hash.hex(),
                                        "old_blinds": old_blind_levels,
                                        "new_blinds": new_blind_levels,
                                        "tournament_state": {
                                            "start_time": new_tournament_state[7],
                                            "blind_timer": new_tournament_state[2],
                                            "last_blind_update": new_tournament_state[3],
                                            "active_players": new_tournament_state[5],
                                            "is_paused": new_tournament_state[8],
                                        },
                                        "timing": {
                                            "current_time": latest_block.timestamp,
                                            "elapsed_time": tournament_elapsed_time,
                                            "level_calculation": f"{tournament_elapsed_time} // {new_tournament_state[2]} = {tournament_elapsed_time // new_tournament_state[2]}"
                                        }
                                    }
                                    
                                    # Try to also get first hand start time from TournamentLogic
                                    try:
                                        # Get tournament logic address from router
                                        tournament_logic_address = self.router.functions.getImplementation(2).call()
                                        
                                        # Load TournamentLogic ABI
                                        with open('abis/TournamentLogic.json', 'r') as f:
                                            tournament_logic_abi = json.load(f)
                                        
                                        # Create contract instance
                                        tournament_logic = self.web3.eth.contract(
                                            address=tournament_logic_address,
                                            abi=tournament_logic_abi
                                        )
                                        
                                        # Call debugGetFirstHandStartTime function
                                        first_hand_start_time = tournament_logic.functions.debugGetFirstHandStartTime().call()
                                        debug_info["first_hand_start_time"] = first_hand_start_time
                                        
                                        if first_hand_start_time > 0:
                                            # Calculate time from first hand
                                            time_since_first_hand = latest_block.timestamp - first_hand_start_time
                                            debug_info["time_since_first_hand"] = time_since_first_hand
                                            debug_info["first_hand_level_calculation"] = f"{time_since_first_hand} // {new_tournament_state[2]} = {time_since_first_hand // new_tournament_state[2]}"
                                    except Exception as e:
                                        debug_info["first_hand_time_error"] = str(e)
                                    
                                    # Use our new enhanced debug info logging with high importance
                                    if TRANSCRIPT_AVAILABLE:
                                        try:
                                            transcript.log_debug_info(
                                                "BLIND LEVEL UPDATE DETAILS", 
                                                debug_info,
                                                "high"
                                            )
                                        except Exception as transcript_e:
                                            logger.error(f"Error logging debug info to transcript: {transcript_e}")
                                    else:
                                        logger.info("Transcript logging disabled - debug info not logged")
                                except Exception as debug_e:
                                    logger.error(f"Error logging enhanced blind debug info: {debug_e}")
                        except Exception as e:
                            logger.error(f"Error logging blind increase to transcript: {e}")
                            
                        return True
                    else:
                        logger.error(f"Blind update transaction failed (tx: {tx_hash.hex()})")
                        if attempt < max_attempts - 1:
                            delay = base_delay * (2 ** attempt)
                            logger.info(f"Retrying in {delay}s (attempt {attempt+1}/{max_attempts})")
                            await asyncio.sleep(delay)
                        else:
                            return False
                except Exception as e:
                    # Detailed error logging
                    logger.error(f"Error in blind update transaction attempt {attempt+1}: {e}")
                    
                    # Check if it's a nonce error and handle specifically
                    error_str = str(e).lower()
                    if "nonce too low" in error_str or "already known" in error_str:
                        logger.info("Nonce issue detected - will retry with updated nonce")
                        # Short delay to let the blockchain state update
                        await asyncio.sleep(1)
                    elif "underpriced" in error_str:
                        logger.info("Transaction underpriced - will retry with higher gas price")
                        # No delay needed as we'll increase gas price on retry
                    else:
                        # For other errors, use exponential backoff
                        if attempt < max_attempts - 1:
                            delay = base_delay * (2 ** attempt)
                            logger.info(f"Retrying in {delay}s (attempt {attempt+1}/{max_attempts})")
                            await asyncio.sleep(delay)
                        else:
                            logger.error(f"Failed to update blinds after {max_attempts} attempts")
                            return False
            
            return False
        except Exception as e:
            logger.error(f"Error updating blind levels: {e}")
            return False

    async def monitor_betting_payouts(self):
        """Monitor betting payouts for tournament winners"""
        # Sleep a bit on startup to allow other processes to initialize
        await asyncio.sleep(5)
        
        # Start with a reasonable block range for historical events
        if not hasattr(self, 'last_payout_block'):
            self.last_payout_block = max(0, self.web3.eth.block_number - 1000)
        
        # Track processed payout events to avoid duplicates
        if not hasattr(self, 'processed_payout_events'):
            self.processed_payout_events = {}  # Format: {event_id: timestamp}
        
        # Initialize betting contract
        try:
            # We need to provide the betting contract address directly since it's not in getImplementation
            # Check if we have it in the config or arguments
            if hasattr(self, 'betting_contract_address') and self.betting_contract_address:
                betting_contract_address = self.betting_contract_address
            else:
                # Try to get a default value from env or use a reasonable default
                logger.warning("No betting contract address provided, using fallback method")
                # In a production setup, you should provide this address explicitly
                
                # DEVELOPMENT FALLBACK: Try to make a direct call to the router to get pokerBettingContract
                # This will only work if the router has a public getter for pokerBettingContract
                try:
                    # This is just a placeholder - Router might not expose this function publicly
                    betting_contract_address = self.router.functions.pokerBettingContract().call()
                    logger.info(f"Got betting contract address from router: {betting_contract_address}")
                except Exception as router_err:
                    logger.error(f"Could not get betting contract address from router: {router_err}")
                    betting_contract_address = None
            
            if betting_contract_address and betting_contract_address != "0x0000000000000000000000000000000000000000":
                # Load PokerBettingContract ABI
                try:
                    with open('abis/PokerBettingContract.json', 'r') as f:
                        betting_contract_abi = json.load(f)
                
                    # Initialize contract
                    self.betting_contract = self.web3.eth.contract(
                        address=betting_contract_address,
                        abi=betting_contract_abi
                    )
                    logger.info(f"Initialized betting contract at {betting_contract_address}")
                except FileNotFoundError:
                    logger.error("PokerBettingContract.json ABI file not found")
                    self.betting_contract = None
            else:
                logger.warning("No valid betting contract address available")
                self.betting_contract = None
        except Exception as e:
            logger.error(f"Failed to initialize betting contract: {e}")
            self.betting_contract = None
        
        while self.is_running:
            try:
                # Skip if we failed to initialize betting contract
                if not self.betting_contract:
                    await asyncio.sleep(30)
                    continue
                
                # Get latest block
                latest_block = self.web3.eth.block_number
                
                # Only proceed if there are new blocks
                if latest_block <= self.last_payout_block:
                    await asyncio.sleep(5)
                    continue
                
                logger.info(f"Checking for payout events from blocks {self.last_payout_block+1} to {latest_block}")
                
                # Look for AdditionalPayoutsNeeded events
                try:
                    # Get event signature
                    event_signature = self.web3.keccak(
                        text="AdditionalPayoutsNeeded(uint256,uint256,uint256)"
                    ).hex()
                    
                    # Get logs
                    logs = self.web3.eth.get_logs({
                        'address': self.betting_contract.address,
                        'fromBlock': self.last_payout_block + 1,
                        'toBlock': latest_block,
                        'topics': [event_signature]
                    })
                    
                    # Process each event
                    for log in logs:
                        # Create unique event ID with standardized format
                        event_id = f"event-payout-{log['blockNumber']}-{log['transactionIndex']}-{log['logIndex']}"
                        
                        # Skip if already processed
                        if event_id in self.processed_payout_events:
                            logger.debug(f"Skipping already processed payout event: {event_id}")
                            continue
                        
                        # Decode event data
                        decoded_log = self.betting_contract.events.AdditionalPayoutsNeeded().process_log(log)
                        tournament_id = decoded_log['args']['tournamentId']
                        remaining_batches = decoded_log['args']['remainingBatches']
                        total_bettors = decoded_log['args']['totalBettors']
                        
                        logger.info(f"Found AdditionalPayoutsNeeded event for tournament {tournament_id}: "
                                   f"{remaining_batches} batches remaining, {total_bettors} bettors total")
                        
                        # Process batches sequentially
                        await self.process_payout_batches(tournament_id, remaining_batches)
                        
                        # Mark as processed with timestamp
                        import time
                        self.processed_payout_events[event_id] = time.time()
                except Exception as e:
                    logger.error(f"Error processing payout events: {e}")
                
                # Update the last processed block
                self.last_payout_block = latest_block
                
                # Clean up processed events dictionary using timestamp-based pruning
                if len(self.processed_payout_events) > 1000:
                    logger.info(f"Pruning processed payout events dictionary (current size: {len(self.processed_payout_events)})")
                    # Sort events by timestamp and keep the 500 newest
                    sorted_events = sorted(self.processed_payout_events.items(), key=lambda x: x[1])
                    self.processed_payout_events = dict(sorted_events[-500:])
                    logger.info(f"Pruned processed payout events to {len(self.processed_payout_events)} entries")
                
                # Sleep between checks
                await asyncio.sleep(10)
                
            except Exception as e:
                logger.error(f"Error in betting payouts monitor: {e}")
                await asyncio.sleep(30)  # Longer sleep on error
    
    def debug_log_timer_state(self, reason="Periodic check"):
        """Log the current state of timers and event processing for debugging"""
        try:
            # Log basic stats about processed events and active timers
            timer_event_count = sum(1 for k in self.processed_events.keys() if k.startswith("event-timer-"))
            action_event_count = sum(1 for k in self.processed_events.keys() if k.startswith("event-action-"))
            manual_timer_count = sum(1 for k in self.processed_events.keys() if k.startswith("event-timer-manual-"))
            
            logger.info(f"===== TIMER DEBUG [{reason}] =====")
            logger.info(f"Active timers: {len(self.active_timers)}")
            logger.info(f"Processed events: {len(self.processed_events)} total")
            logger.info(f"  - Timer events: {timer_event_count}")
            logger.info(f"  - Action events: {action_event_count}")
            logger.info(f"  - Manual timers: {manual_timer_count}")
            
            # Log details about active timers
            if self.active_timers:
                for idx, (player, expiry) in enumerate(self.active_timers.items()):
                    seconds_left = (expiry - datetime.now()).total_seconds()
                    logger.info(f"  Timer {idx+1}: Player {player[:8]}... expires in {seconds_left:.1f}s")
            
            # Log any recent events (last 5 minutes)
            import time
            cutoff_time = time.time() - 300  # Last 5 minutes
            recent_events = [(k, v) for k, v in self.processed_events.items() if v > cutoff_time]
            recent_events.sort(key=lambda x: x[1], reverse=True)  # Sort by timestamp, newest first
            
            if recent_events:
                logger.info(f"Recent events (last 5 min, showing most recent 5):")
                for event_id, timestamp in recent_events[:5]:
                    time_ago = time.time() - timestamp
                    logger.info(f"  {event_id} ({time_ago:.1f}s ago)")
            
            
            logger.info("=============================")
            
            # Also log to transcript for permanent record
            if TRANSCRIPT_AVAILABLE:
                try:
                    transcript.log_custom_event("TIMER_DEBUG_STATE", {
                        "reason": reason,
                        "active_timer_count": len(self.active_timers),
                        "processed_events_total": len(self.processed_events),
                        "timer_events": timer_event_count,
                        "action_events": action_event_count,
                        "manual_timers": manual_timer_count,
                        "recent_event_count": len(recent_events)
                    })
                except Exception as e:
                    logger.error(f"Error logging timer debug state to transcript: {e}")
            else:
                logger.info("Transcript logging disabled - timer debug state not logged")
                
        except Exception as e:
            logger.error(f"Error logging timer debug state: {e}")
            
    def audit_chip_balances(self, reason="Periodic audit"):
        """Audit all player chip balances to detect inflation issues"""
        try:
            # Get tournament state to check if it's active
            tournament_state = self.state_storage.functions.getTournamentStateValues().call()
            if tournament_state[4] != 1:  # 1 = Active TableState
                logger.info(f"Chip audit skipped - tournament not active (state: {tournament_state[4]})")
                return
                
            logger.info(f"===== CHIP BALANCE AUDIT [{reason}] =====")
            
            # Track total chips in the system
            total_chips = 0
            total_pot = 0
            player_chips = {}
            player_count = 0
            
            # Get the main pot
            game_state = self.state_storage.functions.getGameStateValues().call()
            main_pot = game_state[3]  # mainPot
            total_pot = main_pot
            logger.info(f"Main pot: {main_pot}")
            
            # Get side pots if any
            side_pot_count = self.state_storage.functions.sidePotCount().call()
            side_pot_total = 0
            if side_pot_count > 0:
                for i in range(side_pot_count):
                    side_pot = self.state_storage.functions.getSidePot(i).call()
                    side_pot_total += side_pot[0]  # amount
                    logger.info(f"Side pot {i}: {side_pot[0]} (resolved: {side_pot[1]})")
            
            total_pot += side_pot_total
            logger.info(f"Total pot: {total_pot}")
            
            # Get all player stacks
            for i in range(8):  # MAX_PLAYERS is typically 8
                player_address = self.state_storage.functions.getPlayerAtPosition(i).call()
                if player_address and player_address != "0x0000000000000000000000000000000000000000":
                    player = self.state_storage.functions.getPlayer(player_address).call()
                    stack = player[0]  # stack value
                    status = player[1]  # status field
                    status_names = ["INACTIVE", "ACTIVE", "FOLDED", "ELIMINATED", "ALL_IN"]
                    status_text = status_names[status] if 0 <= status < len(status_names) else f"UNKNOWN({status})"
                    
                    # Add to total and record for the player
                    total_chips += stack
                    player_chips[player_address] = {
                        "stack": stack,
                        "status": status_text,
                        "position": i
                    }
                    player_count += 1
                    
                    logger.info(f"Player at position {i}: {player_address[:10]}... Stack: {stack} Status: {status_text}")
            
            # Calculate expected total (assuming 200 starting chips per player)
            starting_chips = 200  # Assuming everyone starts with 200
            expected_total = player_count * starting_chips
            
            # Check for system-wide inflation
            system_total = total_chips + total_pot
            
            if system_total != expected_total:
                inflation_amount = system_total - expected_total
                logger.warning(f"CHIP DISCREPANCY DETECTED: System has {system_total} chips, expected {expected_total}")
                logger.warning(f"Inflation: {inflation_amount} chips")
                
                # Log to transcript for permanent record
                if TRANSCRIPT_AVAILABLE:
                    try:
                        transcript.log_custom_event("CHIP_INFLATION_DETECTED", {
                            "reason": reason,
                            "system_total": system_total,
                            "expected_total": expected_total,
                            "inflation": inflation_amount,
                            "player_count": player_count,
                            "main_pot": main_pot,
                            "side_pots": side_pot_total,
                            "players": player_chips
                        })
                    except Exception as e:
                        logger.error(f"Error logging chip inflation to transcript: {e}")
                else:
                    logger.warning("Transcript logging disabled - chip inflation not logged")
                    
                # If we have inflation, attempt to fix it
                if inflation_amount > 0:
                    self.fix_chip_inflation(inflation_amount, player_chips)
            else:
                logger.info(f"CHIP AUDIT PASSED: System has correct total of {system_total} chips")
                
            logger.info("===========================================================")
            
        except Exception as e:
            logger.error(f"Error during chip balance audit: {e}")
            
    async def process_payout_batches(self, tournament_id, remaining_batches):
        """Process payout batches for a tournament"""
        try:
            logger.info(f"Processing {remaining_batches} payout batches for tournament {tournament_id}")
            
            # Get the total number of batches from contract
            is_complete, total_batches, total_bettors = self.betting_contract.functions.isDistributionComplete(
                tournament_id
            ).call()
            
            if is_complete:
                logger.info(f"Distribution already complete for tournament {tournament_id}")
                return
            
            # Calculate which batches we need to process
            # Batch 0 is processed by the handleTournamentComplete function
            # We need to process batches 1 through total_batches - 1
            for batch in range(1, int(total_batches)):
                # Add throttling to avoid gas issues
                max_attempts = 3
                
                for attempt in range(max_attempts):
                    try:
                        # Check if distribution is now complete before processing this batch
                        is_now_complete, _, _ = self.betting_contract.functions.isDistributionComplete(
                            tournament_id
                        ).call()
                        
                        if is_now_complete:
                            logger.info(f"Distribution completed during processing. Stopping at batch {batch}")
                            return
                        
                        logger.info(f"Processing payout batch {batch} for tournament {tournament_id} (attempt {attempt+1})")
                        
                        # Get fresh nonce for each attempt
                        current_nonce = self.web3.eth.get_transaction_count(self.account.address)
                        gas_price = int(self.web3.eth.gas_price * 1.2)  # Increase gas price by 20%
                        
                        # Build transaction
                        try:
                            tx = self.betting_contract.functions.continueDistributingWinnings(
                                tournament_id,
                                batch
                            ).build_transaction({
                                'from': self.account.address,
                                    'nonce': current_nonce,
                                    'gasPrice': gas_price
                                })
                        except Exception as e:
                            logger.error(f"Error building transaction: {e}. Fallback to 4000000 gas.")
                            tx = self.betting_contract.functions.continueDistributingWinnings(
                                tournament_id,
                                batch
                            ).build_transaction({
                                'from': self.account.address,
                                'nonce': current_nonce,
                                'gasPrice': gas_price,
                                'gas': 4000000
                            })
                        
                        # Sign and send transaction
                        signed_tx = self.web3.eth.account.sign_transaction(tx, self.account.key)
                        tx_hash = self.web3.eth.send_raw_transaction(signed_tx.rawTransaction)
                        
                        # Wait for receipt with longer timeout
                        receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
                        
                        if receipt['status'] == 1:
                            logger.info(f"Successfully processed payout batch {batch} for tournament {tournament_id} "
                                       f"(tx: {tx_hash.hex()})")
                            break  # Success, move to next batch
                        else:
                            logger.error(f"Payout transaction failed for batch {batch} (tx: {tx_hash.hex()})")
                            
                            if attempt < max_attempts - 1:
                                # Exponential backoff
                                sleep_time = 5 * (2 ** attempt)
                                logger.info(f"Retrying in {sleep_time}s (attempt {attempt+1}/{max_attempts})")
                                await asyncio.sleep(sleep_time)
                            else:
                                logger.error(f"Failed to process batch {batch} after {max_attempts} attempts. "
                                           f"Moving to next batch.")
                    
                    except Exception as e:
                        logger.error(f"Error processing batch {batch} (attempt {attempt+1}): {e}")
                        
                        if attempt < max_attempts - 1:
                            # Exponential backoff
                            sleep_time = 5 * (2 ** attempt)
                            logger.info(f"Retrying in {sleep_time}s (attempt {attempt+1}/{max_attempts})")
                            await asyncio.sleep(sleep_time)
                        else:
                            logger.error(f"Failed to process batch {batch} after {max_attempts} attempts. "
                                       f"Moving to next batch.")
                
                # Sleep between batches to avoid network congestion
                await asyncio.sleep(3)
            
            # Final check to confirm all batches are processed
            is_complete, _, _ = self.betting_contract.functions.isDistributionComplete(
                tournament_id
            ).call()
            
            if is_complete:
                logger.info(f"Successfully completed all payout distributions for tournament {tournament_id}")
            else:
                logger.warning(f"Some payouts may still be pending for tournament {tournament_id}")
                
        except Exception as e:
            logger.error(f"Error in process_payout_batches for tournament {tournament_id}: {e}")

    
    async def start(self):
        """Start the timer agent"""
        self.is_running = True
        logger.info("Starting timer agent...")
        
        try:
            # Run all monitoring methods concurrently
            await asyncio.gather(
                self.monitor_events(),
                self.monitor_timers(),
                self.monitor_blind_levels(),
                self.monitor_betting_payouts()
            )
        except Exception as e:
            logger.error(f"Error in timer agent main loop: {e}")
            self.is_running = False

    async def stop(self):
        """Stop the timer agent and ensure all tasks are cancelled"""
        logger.info("Stopping timer agent...")
        self.is_running = False
        self.active_timers.clear()
        
        # Give time for tasks to recognize the stopped state
        await asyncio.sleep(0.5)
        
        # Cancel all tasks associated with this agent
        tasks = [t for t in asyncio.all_tasks() 
                if t != asyncio.current_task() and 
                "timer_agent" in str(t) and not t.done()]
        
        if tasks:
            logger.info(f"Cancelling {len(tasks)} timer agent tasks")
            for task in tasks:
                task.cancel()
            
            # Wait for tasks to be cancelled
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
                logger.info("All timer agent tasks cancelled successfully")
            except Exception as e:
                logger.error(f"Error cancelling timer agent tasks: {e}")
        else:
            logger.info("No timer agent tasks to cancel")
