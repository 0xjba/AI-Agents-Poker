import os
import json
import logging
import time
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Union
from .constants import CURRENT_TRANSCRIPT_FILE, BettingRound, PlayerAction

# Set up a specialized logger for detailed transcript logging
transcript_logger = logging.getLogger('transcript')
transcript_logger.setLevel(logging.DEBUG)

# Configure file handler for the transcript logger
os.makedirs('logs/transcripts', exist_ok=True)
timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
log_file_path = os.path.join('logs', f"poker_game_{timestamp}.log")

# Create a file handler that logs to a single log file
file_handler = logging.FileHandler(log_file_path)
file_handler.setLevel(logging.DEBUG)

# Create formatter with timestamp, level, and message
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)

# Add the handler to the logger
transcript_logger.addHandler(file_handler)

# Configure the root logger as well to capture all logs
root_logger = logging.getLogger()
root_logger.addHandler(file_handler)

# Configure terminal UI logging
terminal_log_handler = logging.FileHandler(log_file_path)
terminal_log_handler.setFormatter(formatter)
terminal_logger = logging.getLogger('terminal_ui')
terminal_logger.addHandler(terminal_log_handler)
terminal_logger.setLevel(logging.DEBUG)

# Standard logger for regular activity
logger = logging.getLogger(__name__)
logger.addHandler(file_handler)

# Card representation utilities
RANKS = {
    1: "A", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7",
    8: "8", 9: "9", 10: "10", 11: "J", 12: "Q", 13: "K"
}
SUITS = {
    0: "♠", 1: "♥", 2: "♦", 3: "♣"
}

class TranscriptManager:
    def __init__(self, file_path=None):
        """Initialize transcript manager with a streamlined logging approach"""
        self.current_hand = 0
        self.tournament_id = 0
        
        # We don't need to initialize a transcript file anymore
        # All logs will go to the single log file configured above
        
        transcript_logger.info("Simplified logging system initialized")
    
    def _initialize_transcript(self):
        """Create the transcript file with a header"""
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"""
=======================================================================
                POKER TOURNAMENT TRANSCRIPT
=======================================================================
Date: {timestamp}
Tournament ID: {self.tournament_id}

This transcript records all actions, decisions, and rationales during
the poker tournament. Each hand is documented with betting rounds, 
player actions, and AI reasoning.
=======================================================================

"""
        try:
            with open(self.file_path, 'w') as f:
                f.write(header)
            logger.info(f"Initialized transcript file at {self.file_path}")
        except Exception as e:
            logger.error(f"Error creating transcript file: {e}")
    
    def card_to_string(self, card_code: int) -> str:
        """Convert card code to readable string (e.g., 26 -> J♥)"""
        if card_code == 0:
            return "??"
        
        # Card encoding: suit (2 bits) + rank (4 bits)
        suit = (card_code - 1) % 4
        rank = (card_code - 1) // 4 + 1
        
        return f"{RANKS.get(rank, str(rank))}{SUITS.get(suit, '?')}"
    
    def cards_to_string(self, cards: List[int]) -> str:
        """Convert a list of card codes to a readable string"""
        return ' '.join([self.card_to_string(card) for card in cards if card > 0])
    
    def log_tournament_start(self, player_addresses: List[str], blind_amounts: Dict[str, int]):
        """Log the start of a tournament"""
        try:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.tournament_id += 1
            
            # Format player information
            player_list = "\n".join([f"  - Player {i+1}: {addr[:10]}...{addr[-6:]}" 
                                    for i, addr in enumerate(player_addresses)])
            
            entry = f"""
-----------------------------------------------------------------------
TOURNAMENT {self.tournament_id} STARTED - {timestamp}
-----------------------------------------------------------------------
Players:
{player_list}

Starting Blinds:
  - Small Blind: {blind_amounts.get('small', 25)}
  - Big Blind: {blind_amounts.get('big', 50)}
-----------------------------------------------------------------------
"""
            with open(self.file_path, 'a') as f:
                f.write(entry)
                
            logger.info(f"Logged tournament start to transcript")
            
        except Exception as e:
            logger.error(f"Error logging tournament start: {e}")
    
    def log_hand_start(self, button_position: int, 
                      players_info: List[Dict[str, Any]],
                      blind_info: Dict[str, Any]):
        """Log the start of a new hand"""
        try:
            self.current_hand += 1
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Format player information
            player_list = ""
            for p in players_info:
                addr = p.get('address', '0x0')
                short_addr = f"{addr[:10]}...{addr[-6:]}" if len(addr) > 16 else addr
                stack = p.get('stack', 0)
                position = p.get('position', -1)
                status = p.get('status', 'Unknown')
                
                # Format hole cards if available
                hole_cards = p.get('hole_cards', [0, 0])
                cards_str = self.cards_to_string(hole_cards) if any(c > 0 for c in hole_cards) else "?? ??"
                
                player_list += f"  - Position {position}: {short_addr} - Stack: {stack} - Cards: {cards_str} - Status: {status}\n"
            
            entry = f"""
-----------------------------------------------------------------------
HAND #{self.current_hand} - {timestamp}
-----------------------------------------------------------------------
Button Position: {button_position}
Small Blind: {blind_info.get('small', 25)}
Big Blind: {blind_info.get('big', 50)}
Current Blind Level: {blind_info.get('level', 0)}

Players:
{player_list}
"""
            with open(self.file_path, 'a') as f:
                f.write(entry)
                
            logger.info(f"Logged start of hand #{self.current_hand} to transcript")
            
        except Exception as e:
            logger.error(f"Error logging hand start: {e}")
    
    def log_betting_round(self, round_name: Union[BettingRound, str], community_cards: List[int] = None):
        """Log the start of a betting round"""
        try:
            # Convert enum to string if needed
            if isinstance(round_name, BettingRound):
                round_names = {
                    BettingRound.PREFLOP: "PREFLOP",
                    BettingRound.FLOP: "FLOP",
                    BettingRound.TURN: "TURN",
                    BettingRound.RIVER: "RIVER"
                }
                round_str = round_names.get(round_name, str(round_name))
            else:
                round_str = str(round_name)
            
            # Format community cards if provided
            cards_str = ""
            if community_cards:
                cards_str = f" - Community Cards: {self.cards_to_string(community_cards)}"
            
            entry = f"""
*** {round_str} ***{cards_str}
"""
            with open(self.file_path, 'a') as f:
                f.write(entry)
                
            logger.info(f"Logged start of {round_str} betting round to transcript")
            
        except Exception as e:
            logger.error(f"Error logging betting round: {e}")
    
    def log_player_action(self, player_address: str, action: Union[PlayerAction, str], 
                         amount: int = 0, reasoning: str = None, 
                         is_ai: bool = False, stack_before: int = None,
                         stack_after: int = None):
        """Log a player action with optional AI reasoning"""
        try:
            # Handle both enum and string action types
            if isinstance(action, PlayerAction):
                action_str = action.value
            else:
                action_str = str(action)
            
            # Format the action including amount if needed
            if action_str in ["BET", "RAISE", "CALL"] and amount > 0:
                action_display = f"{action_str} {amount}"
            elif action_str == "BLIND":
                action_display = f"{action_str} {amount}"
            else:
                action_display = action_str
            
            # Format player address for display
            short_addr = f"{player_address[:10]}...{player_address[-6:]}" if len(player_address) > 16 else player_address
            
            # Format stack change if provided
            stack_info = ""
            if stack_before is not None and stack_after is not None:
                stack_diff = stack_after - stack_before
                stack_info = f" (Stack: {stack_before} → {stack_after}, Δ{stack_diff:+})"
            
            # Basic action entry
            entry = f"{short_addr}: {action_display}{stack_info}\n"
            
            # Add AI reasoning if provided and it's an AI player
            if is_ai and reasoning:
                # Format reasoning with proper indentation
                formatted_reasoning = '\n'.join(['    ' + line for line in reasoning.strip().split('\n')])
                entry += f"    Reasoning: {formatted_reasoning}\n"
            
            with open(self.file_path, 'a') as f:
                f.write(entry)
                
            logger.info(f"Logged player action: {short_addr} {action_display}")
            
        except Exception as e:
            logger.error(f"Error logging player action: {e}")
    
    def log_hand_result(self, winners: List[Dict[str, Any]], 
                       pot_amount: int, 
                       final_board: List[int] = None,
                       showdown_info: List[Dict[str, Any]] = None):
        """Log the result of a hand including winners and showdowns"""
        try:
            # Format the board if provided
            board_str = ""
            if final_board:
                board_str = f"Final Board: {self.cards_to_string(final_board)}\n"
            
            # Format showdown information if provided
            showdown_str = ""
            if showdown_info:
                showdown_str = "Showdown:\n"
                for player in showdown_info:
                    addr = player.get('address', '0x0')
                    short_addr = f"{addr[:10]}...{addr[-6:]}" if len(addr) > 16 else addr
                    cards = player.get('hole_cards', [0, 0])
                    cards_str = self.cards_to_string(cards)
                    hand_name = player.get('hand_name', 'Unknown Hand')
                    showdown_str += f"  - {short_addr}: {cards_str} ({hand_name})\n"
            
            # Format winner information
            winners_str = "Winner(s):\n"
            for winner in winners:
                addr = winner.get('address', '0x0')
                short_addr = f"{addr[:10]}...{addr[-6:]}" if len(addr) > 16 else addr
                amount = winner.get('amount', 0)
                winners_str += f"  - {short_addr} wins {amount}\n"
            
            entry = f"""
{board_str}{showdown_str}
{winners_str}
Pot: {pot_amount}
-----------------------------------------------------------------------
"""
            with open(self.file_path, 'a') as f:
                f.write(entry)
                
            logger.info(f"Logged hand result to transcript")
            
        except Exception as e:
            logger.error(f"Error logging hand result: {e}")
    
    def log_blind_increase(self, old_levels: Dict[str, int], new_levels: Dict[str, int], reason: str = None):
        """Log blind level increases"""
        try:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            old_sb = old_levels.get('small', 0)
            old_bb = old_levels.get('big', 0)
            old_level = old_levels.get('level', 0)
            
            new_sb = new_levels.get('small', 0)
            new_bb = new_levels.get('big', 0)
            new_level = new_levels.get('level', 0)
            
            reason_str = f"\nReason: {reason}" if reason else ""
            
            entry = f"""
BLIND INCREASE - {timestamp}
  Level {old_level} → {new_level}
  Small Blind: {old_sb} → {new_sb}
  Big Blind: {old_bb} → {new_bb}{reason_str}
"""
            with open(self.file_path, 'a') as f:
                f.write(entry)
                
            logger.info(f"Logged blind increase to transcript")
            
        except Exception as e:
            logger.error(f"Error logging blind increase: {e}")
    
    def log_tournament_end(self, winner_address: str, duration_seconds: int, final_stats: Dict[str, Any] = None):
        """Log the end of a tournament"""
        try:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Format duration
            hours = duration_seconds // 3600
            minutes = (duration_seconds % 3600) // 60
            seconds = duration_seconds % 60
            duration_str = f"{hours}h {minutes}m {seconds}s"
            
            # Format winner address
            short_addr = f"{winner_address[:10]}...{winner_address[-6:]}" if len(winner_address) > 16 else winner_address
            
            # Format additional statistics if provided
            stats_str = ""
            if final_stats:
                stats_str = "Tournament Statistics:\n"
                for key, value in final_stats.items():
                    stats_str += f"  - {key}: {value}\n"
            
            entry = f"""
=======================================================================
TOURNAMENT COMPLETED - {timestamp}
=======================================================================
Winner: {short_addr}
Duration: {duration_str}
Total Hands Played: {self.current_hand}

{stats_str}
=======================================================================
"""
            with open(self.file_path, 'a') as f:
                f.write(entry)
                
            logger.info(f"Logged tournament end to transcript")
            
        except Exception as e:
            logger.error(f"Error logging tournament end: {e}")
    
    def log_custom_event(self, event_type: str, details: Dict[str, Any] = None, formatted_text: str = None):
        """Log a custom event directly to the log file"""
        try:
            # Log the main event type
            transcript_logger.info(f"EVENT: {event_type}")
            
            # If formatted text is provided, log it directly
            if formatted_text:
                transcript_logger.info(formatted_text)
            
            # Otherwise log the details
            elif details:
                # For cleaner logging, convert details to JSON string if there are multiple items
                if len(details) > 3:
                    transcript_logger.info(f"DETAILS: {json.dumps(details)}")
                else:
                    # For a few items, log each on separate lines for readability
                    for key, value in details.items():
                        transcript_logger.info(f"{key}: {value}")
                        
            logger.debug(f"Logged custom event: {event_type}")
            
        except Exception as e:
            logger.error(f"Error logging custom event: {e}")
            
    def log_timeout(self, player_address: str, success: bool, player_positions: List[Dict[str, Any]] = None):
        """Log a player timeout with detailed position information"""
        try:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            short_addr = f"{player_address[:10]}...{player_address[-6:]}" if len(player_address) > 16 else player_address
            
            # Format status message
            status = "SUCCEEDED" if success else "FAILED"
            
            # Create main timeout entry
            entry = f"""
-----------------------------------------------------------------------
TIMEOUT EVENT - {timestamp}
-----------------------------------------------------------------------
Player: {short_addr}
Status: {status}
"""
            
            # Add detailed player position information if provided
            if player_positions:
                entry += "\nPlayer Positions:\n"
                for player in player_positions:
                    addr = player.get('address', '0x0')
                    short_addr = f"{addr[:10]}...{addr[-6:]}" if len(addr) > 16 else addr
                    position = player.get('position', -1)
                    status = player.get('status', 'Unknown')
                    stack = player.get('stack', 0)
                    
                    entry += f"  - Position {position}: {short_addr} - Status: {status} - Stack: {stack}\n"
            
            entry += "-----------------------------------------------------------------------\n"
            
            with open(self.file_path, 'a') as f:
                f.write(entry)
                
            logger.info(f"Logged timeout event for player {short_addr}, success={success}")
            
        except Exception as e:
            logger.error(f"Error logging timeout: {e}")
            
    def log_multiple_timers(self, active_timers: Dict[str, Any], current_turn: str = None):
        """Log a multiple timers issue for debugging"""
        try:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Create entry header
            entry = f"""
-----------------------------------------------------------------------
MULTIPLE TIMERS DETECTED - {timestamp}
-----------------------------------------------------------------------
Active Timer Count: {len(active_timers)}
Current Turn: {current_turn if current_turn else "Unknown"}

Active Timers:
"""
            # Add each timer's information
            for player_addr, expiry_time in active_timers.items():
                short_addr = f"{player_addr[:10]}...{player_addr[-6:]}" if len(player_addr) > 16 else player_addr
                time_left = (expiry_time - datetime.datetime.now()).total_seconds()
                entry += f"  - Player: {short_addr} - Expires in: {time_left:.1f}s\n"
            
            entry += "-----------------------------------------------------------------------\n"
            
            with open(self.file_path, 'a') as f:
                f.write(entry)
                
            logger.warning(f"Logged multiple timers event with {len(active_timers)} timers")
            
        except Exception as e:
            logger.error(f"Error logging multiple timers: {e}")
            
    def log_position_change(self, player_address: str, old_position: int, new_position: int, reason: str = None):
        """Log when a player's position changes unexpectedly"""
        try:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            short_addr = f"{player_address[:10]}...{player_address[-6:]}" if len(player_address) > 16 else player_address
            
            entry = f"""
-----------------------------------------------------------------------
PLAYER POSITION CHANGE - {timestamp}
-----------------------------------------------------------------------
Player: {short_addr}
Position Change: {old_position} → {new_position}
"""
            
            if reason:
                entry += f"Reason: {reason}\n"
                
            entry += "-----------------------------------------------------------------------\n"
            
            with open(self.file_path, 'a') as f:
                f.write(entry)
                
            logger.warning(f"Logged position change for player {short_addr}: {old_position} → {new_position}")
            
        except Exception as e:
            logger.error(f"Error logging position change: {e}")

    def log_contract_event(self, event_name: str, event_data: Dict[str, Any], tx_hash: str = None):
        """Log detailed blockchain contract events for debugging"""
        try:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Format the transaction hash if provided
            tx_str = f"Transaction: {tx_hash}\n" if tx_hash else ""
            
            # Start building the entry
            entry = f"""
-----------------------------------------------------------------------
CONTRACT EVENT: {event_name} - {timestamp}
-----------------------------------------------------------------------
{tx_str}"""
            
            # Add each field from the event data with proper formatting
            entry += "Event Data:\n"
            
            # Loop through all fields in the event data
            for key, value in event_data.items():
                # Format addresses for better readability
                if isinstance(value, str) and value.startswith('0x') and len(value) >= 40:
                    short_value = f"{value[:10]}...{value[-6:]}"
                    entry += f"  - {key}: {short_value}\n"
                # Format timestamps with human-readable date
                elif key.lower().endswith('time') or key.lower() == 'timestamp':
                    try:
                        date_str = datetime.datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
                        entry += f"  - {key}: {value} ({date_str})\n"
                    except (ValueError, TypeError):
                        entry += f"  - {key}: {value}\n"
                # Format blind levels with proper context
                elif key.lower().endswith('blind'):
                    entry += f"  - {key}: {value}\n"
                # Default formatting for other values
                else:
                    entry += f"  - {key}: {value}\n"
            
            entry += "-----------------------------------------------------------------------\n"
            
            with open(self.file_path, 'a') as f:
                f.write(entry)
                
            logger.info(f"Logged contract event: {event_name} with {len(event_data)} data points")
            
        except Exception as e:
            logger.error(f"Error logging contract event: {e}")
    
    def log_debug_info(self, title: str, info: Dict[str, Any], importance: str = "medium"):
        """Log detailed debug information with highlighting based on importance"""
        try:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Format header based on importance
            if importance.lower() == "high":
                header = "======================================================================="
                footer = "======================================================================="
            elif importance.lower() == "medium":
                header = "-----------------------------------------------------------------------"
                footer = "-----------------------------------------------------------------------"
            else:
                header = "- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -"
                footer = "- - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -"
            
            # Start building the entry
            entry = f"""
{header}
DEBUG INFO: {title} - {timestamp}
{header}
"""
            
            # Add each piece of debug information
            for key, value in info.items():
                # For dictionaries, format them with nested indentation
                if isinstance(value, dict):
                    entry += f"{key}:\n"
                    for sub_key, sub_value in value.items():
                        entry += f"  - {sub_key}: {sub_value}\n"
                # For lists, format each item on a new line
                elif isinstance(value, list):
                    entry += f"{key}:\n"
                    for item in value:
                        entry += f"  - {item}\n"
                # For simple values, format them directly
                else:
                    entry += f"{key}: {value}\n"
            
            entry += f"{footer}\n"
            
            with open(self.file_path, 'a') as f:
                f.write(entry)
            
            # Also log to the structured JSON log
            self._log_to_json({
                "type": "DEBUG_INFO",
                "title": title,
                "importance": importance,
                "data": info,
                "timestamp": time.time()
            })
                
            logger.info(f"Logged debug info: {title} with {len(info)} items")
            transcript_logger.info(f"DEBUG: {title} - {json.dumps(info)}")
            
        except Exception as e:
            logger.error(f"Error logging debug info: {e}")
            transcript_logger.error(f"Error logging debug info: {e}")
    
    def _log_to_json(self, data: Dict[str, Any]):
        """Log structured data to log file as JSON string"""
        try:
            # Add a timestamp if not present
            if "timestamp" not in data:
                data["timestamp"] = time.time()
                
            # Log the data as a JSON string
            transcript_logger.info(f"JSON_DATA: {json.dumps(data)}")
        except Exception as e:
            logger.error(f"Error logging JSON data: {e}")
    
    def log_contract_state(self, state_data: Dict[str, Any], context: str = ""):
        """Log detailed blockchain contract state for debugging"""
        try:
            # Log only to debug logs - too verbose for transcript
            data = {
                "type": "CONTRACT_STATE",
                "context": context,
                "state": state_data,
                "timestamp": time.time()
            }
            
            # Add to structured JSON log
            self._log_to_json(data)
            
            # Log to debug log as well
            transcript_logger.debug(f"Contract state ({context}): {json.dumps(state_data)}")
            
        except Exception as e:
            logger.error(f"Error logging contract state: {e}")
            transcript_logger.error(f"Error logging contract state: {e}")
    
    def log_game_state(self, round_type: str, round_number: int, current_turn: str, pot_size: int, 
                      current_bet: int, community_cards: List[int] = None):
        """Log current game state for debugging and analysis"""
        try:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Format community cards if present
            cards_str = self.cards_to_string(community_cards) if community_cards else "N/A"
            
            # Format current turn player
            turn_str = f"{current_turn[:8]}...{current_turn[-6:]}" if current_turn and len(current_turn) > 14 else current_turn
            
            # Build data object
            state_data = {
                "type": "GAME_STATE",
                "timestamp": time.time(),
                "round_type": round_type,
                "round_number": round_number,
                "current_turn": current_turn,
                "shortened_turn": turn_str,
                "pot_size": pot_size,
                "current_bet": current_bet,
                "community_cards": cards_str,
                "human_time": timestamp
            }
            
            # Log to structured JSON
            self._log_to_json(state_data)
            
            # Log to debug log
            transcript_logger.info(f"Game state: Round={round_type}({round_number}), Turn={turn_str}, Pot={pot_size}, Bet={current_bet}, Cards={cards_str}")
            
        except Exception as e:
            logger.error(f"Error logging game state: {e}")
            transcript_logger.error(f"Error logging game state: {e}")
    
    def log_timeout_failure(self, player_address: str, reason: str, tx_hash: str = None, context: Dict[str, Any] = None):
        """Log detailed timeout failures with context information to log file"""
        try:
            # Format player address
            short_addr = f"{player_address[:8]}...{player_address[-6:]}" if len(player_address) > 14 else player_address
            
            # Log the main error with reason, player and tx hash
            transcript_logger.error(f"TIMEOUT FAILURE: Player={short_addr}, Reason={reason}, TX={tx_hash if tx_hash else 'unknown'}")
            
            # Extract and log revert reason if available
            if context and "revert_reason" in context:
                transcript_logger.error(f"CONTRACT REVERT: {context['revert_reason']}")
                
            # Log player status details
            if context and "player_status" in context:
                status_name = "UNKNOWN"
                status = context.get("player_status")
                if status == 0:
                    status_name = "INACTIVE"
                elif status == 1:
                    status_name = "ACTIVE"
                elif status == 2:
                    status_name = "FOLDED"
                elif status == 3:
                    status_name = "ELIMINATED"
                elif status == 4:
                    status_name = "ALL_IN"
                transcript_logger.info(f"Player status: {status} ({status_name})")
            
            # Log turn mismatch if it exists
            if context and "current_turn" in context and context["current_turn"] and context["current_turn"] != player_address:
                current_turn = context["current_turn"]
                current_turn_short = f"{current_turn[:8]}...{current_turn[-6:]}" if len(current_turn) > 14 else current_turn
                transcript_logger.warning(f"Turn mismatch: Expected {short_addr}, actual turn is {current_turn_short}")
            
            # Log game state context
            if context:
                state_data = {}
                for key in ["round", "player_stack", "current_bet", "pot_size"]:
                    if key in context:
                        state_data[key] = context[key]
                
                # Only log if we have data
                if state_data:
                    transcript_logger.info(f"Game state: {json.dumps(state_data)}")
            
            # Log any remaining context as debug
            if context:
                remaining_context = {}
                for key, value in context.items():
                    if key not in ["revert_reason", "player_status", "player_stack", "current_turn", "round", "current_bet", "pot_size", "tx_hash", "reason"]:
                        remaining_context[key] = value
                
                if remaining_context:
                    transcript_logger.debug(f"Additional context: {json.dumps(remaining_context)}")
                    
        except Exception as e:
            logger.error(f"Error logging timeout failure: {e}")
            transcript_logger.error(f"Error logging timeout failure: {e}")
    
    def log_all_in_status(self, player_address: str, stack_before: int, current_bet: int, pot_amount: int):
        """Log when a player goes all-in, with detailed context for debugging"""
        try:
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Format player address
            short_addr = f"{player_address[:8]}...{player_address[-6:]}" if len(player_address) > 14 else player_address
            
            # Build entry for transcript
            entry = f"""
PLAYER ALL-IN - {timestamp}
  - player: {short_addr}
  - stack_before: {stack_before}
  - current_bet: {current_bet}
  - pot_amount: {pot_amount}
"""
            
            with open(self.file_path, 'a') as f:
                f.write(f"\n{entry}\n")
                
            # Build data for structured logging
            log_data = {
                "type": "PLAYER_ALL_IN",
                "player": player_address,
                "stack_before": stack_before,
                "current_bet": current_bet,
                "pot_amount": pot_amount,
                "timestamp": time.time()
            }
            
            # Log to structured JSON
            self._log_to_json(log_data)
            
            # Log to debug log
            transcript_logger.info(f"Player all-in: {short_addr} with {stack_before} chips, bet={current_bet}, pot={pot_amount}")
            
        except Exception as e:
            logger.error(f"Error logging all-in status: {e}")
            transcript_logger.error(f"Error logging all-in status: {e}")
    
    def log_stack_trace(self, exception: Exception, context: str = ""):
        """Log stack traces from exceptions for debugging"""
        try:
            import traceback
            
            # Get stack trace as string
            stack_trace = traceback.format_exc()
            
            # Log only to debug log to keep transcript clean
            log_data = {
                "type": "STACK_TRACE",
                "exception": str(exception),
                "stack_trace": stack_trace,
                "context": context,
                "timestamp": time.time()
            }
            
            # Log to structured JSON
            self._log_to_json(log_data)
            
            # Log to debug log
            transcript_logger.error(f"Exception in {context}: {str(exception)}\n{stack_trace}")
            
        except Exception as e:
            logger.error(f"Error logging stack trace: {e}")
            transcript_logger.error(f"Error logging stack trace: {e}")

# Create a singleton instance for global use
transcript = TranscriptManager()