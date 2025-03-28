import os
import sys
import time
import asyncio
import threading
from datetime import datetime
from typing import Dict, List, Optional, Any
from tabulate import tabulate
import logging
import queue
import curses
from enum import Enum

# Configure terminal UI logger
logger = logging.getLogger('terminal_ui')

class LogLevel(Enum):
    INFO = 1
    WARNING = 2
    ERROR = 3
    SUCCESS = 4

class TerminalUI:
    def __init__(self):
        # Screen sections
        self.active_agents_section = {}
        self.timer_section = {}
        self.game_state_section = {}
        self.transaction_history = []
        self.active_events = []
        self.log_messages = []
        
        # Flags and counters
        self.is_running = False
        self.screen = None
        self.max_log_messages = 5    
        self.max_transactions = 5    
        self.max_events = 5          
        
        # Message queue for thread-safe logging
        self.message_queue = queue.Queue()
        
        # References to external objects
        self.agents = {}
        self.timer_agent = None
        
        # Stats
        self.start_time = datetime.now()
        self.hands_played = 0
        self.actions_taken = 0
        self.timeouts_handled = 0
        self.transactions_sent = 0
        self.transactions_failed = 0
        self.current_blinds = (0, 0)
        
        # Removed setup_file_logging call to fix error
        
    def set_agents(self, agents: Dict[str, Any]):
        """Register poker agents for monitoring"""
        self.agents = agents
        
    def set_timer_agent(self, timer_agent: Any):
        """Register timer agent for monitoring"""
        self.timer_agent = timer_agent
    
    def add_log(self, message: str, level: LogLevel = LogLevel.INFO):
        """Add a log message to the UI (thread-safe) and log it to our file"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        # Add to message queue for thread safety
        self.message_queue.put((timestamp, message, level))
        
        # Log to file based on level
        if level == LogLevel.ERROR:
            logger.error(f"UI: {message}")
        elif level == LogLevel.WARNING:
            logger.warning(f"UI: {message}")
        elif level == LogLevel.SUCCESS:
            logger.info(f"UI-SUCCESS: {message}")
        else:
            logger.info(f"UI: {message}")
        
        # For debugging, also add directly to log_messages to ensure visibility
        # This bypasses the regular queue processing but ensures logs appear
        try:
            # Print to console for direct visibility during development
            print(f"DIRECT LOG: {timestamp} | {message} | {level}")
            
            # Add to UI message list
            self.log_messages.insert(0, [timestamp, message, level])
            if len(self.log_messages) > self.max_log_messages:
                self.log_messages.pop()
        except Exception as e:
            print(f"Error adding direct log: {e}")
            logger.error(f"Error adding direct log: {e}")
    
    def add_transaction(self, tx_hash: str, action: str, status: str, details: str = ""):
        """Add a transaction to the history and log it"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.message_queue.put(("TX", timestamp, tx_hash, action, status, details))
        
        # Log transaction to file
        short_hash = tx_hash[:8] + "..." + tx_hash[-6:] if len(tx_hash) > 14 else tx_hash
        log_message = f"TRANSACTION: {short_hash} | {action} | {status} | {details}"
        
        if status.lower() == "failed":
            logger.error(log_message)
        elif status.lower() == "pending":
            logger.info(log_message)
        else:
            logger.info(log_message)
    
    def add_event(self, event_type: str, details: str):
        """Add a game event and log it"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.message_queue.put(("EVENT", timestamp, event_type, details))
        
        # Log event to file
        logger.info(f"EVENT: {event_type} | {details}")
    
    def update_game_state(self, round_name: str, pot_size: int, current_player: str, 
                          blinds: tuple, hand_start_time: int):
        """Update the current game state display and log it"""
        self.message_queue.put(("GAME_STATE", round_name, pot_size, current_player, blinds, hand_start_time))
        
        # Format current player for readability
        short_player = current_player
        if current_player and len(current_player) > 14:
            short_player = f"{current_player[:8]}...{current_player[-6:]}"
            
        # Calculate hand duration
        hand_time = 0
        if hand_start_time > 0:
            hand_time = int(time.time() - hand_start_time)
            
        # Log game state update to file
        logger.info(f"GAME STATE: Round={round_name} | Pot={pot_size} | Player={short_player} | Time={hand_time}s | Blinds={blinds[0]}/{blinds[1]}")
    
    def _process_message(self, msg):
        """Process messages from the queue"""
        try:
            # First, add a direct debug print to see what messages are being processed
            print(f"DEBUG PROCESSING MESSAGE: {str(msg)[:100]}...")
            
            if isinstance(msg, tuple):
                # Check if this is one of our special message types
                if isinstance(msg[0], str):
                    if msg[0] == "TX":
                        # Transaction message
                        _, timestamp, tx_hash, action, status, details = msg
                        short_hash = tx_hash[:8] + "..." + tx_hash[-6:]
                        self.transaction_history.insert(0, [timestamp, short_hash, action, status, details])
                        if len(self.transaction_history) > self.max_transactions:
                            self.transaction_history.pop()
                        
                        # Update stats
                        self.transactions_sent += 1
                        if status.lower() == "failed":
                            self.transactions_failed += 1
                    
                    elif msg[0] == "EVENT":
                        # Event message
                        _, timestamp, event_type, details = msg
                        self.active_events.insert(0, [timestamp, event_type, details])
                        if len(self.active_events) > self.max_events:
                            self.active_events.pop()
                        
                        # Update stats based on event type
                        if event_type == "NEW_HAND":
                            self.hands_played += 1
                        elif event_type == "ACTION":
                            self.actions_taken += 1
                        elif event_type == "TIMEOUT":
                            self.timeouts_handled += 1
                    
                    elif msg[0] == "GAME_STATE":
                        # Game state update
                        _, round_name, pot_size, current_player, blinds, hand_start_time = msg
                        self.game_state_section = {
                            "round": round_name,
                            "pot": pot_size,
                            "current_player": current_player,
                            "hand_start_time": hand_start_time
                        }
                        self.current_blinds = blinds
                    else:
                        # Unknown message type - try to treat as log message
                        print(f"Unknown message type: {msg[0]} - trying to handle as log")
                        if len(msg) == 3:
                            timestamp, message, level = msg
                            self.log_messages.insert(0, [timestamp, message, level])
                            if len(self.log_messages) > self.max_log_messages:
                                self.log_messages.pop()
                else:
                    # This is likely a normal log message
                    if len(msg) == 3:
                        timestamp, message, level = msg
                        print(f"Adding log: {timestamp} | {message[:30]}... | {level}")
                        self.log_messages.insert(0, [timestamp, message, level])
                        if len(self.log_messages) > self.max_log_messages:
                            self.log_messages.pop()
                    else:
                        print(f"Unexpected tuple format: {msg}")
            else:
                # Unexpected message format
                print(f"Unexpected message format: {msg}")
        except Exception as e:
            # Last resort debug
            print(f"Error processing message: {e}, msg={str(msg)[:100]}...")
    
    def _refresh_agent_stats(self):
        """Refresh agent statistics from registered agents"""
        if not self.agents:
            return
            
        self.active_agents_section = {}
        for name, agent in self.agents.items():
            if hasattr(agent, 'is_running') and not agent.is_running:
                continue
                
            address = agent.account.address if hasattr(agent, 'account') else "Unknown"
            address = address[:8] + "..." + address[-6:] if address != "Unknown" else "Unknown"
            
            stack = "Unknown"
            status = "Unknown"
            last_action = "Never"
            
            # Try to get more details
            try:
                if hasattr(agent, 'last_action_time') and agent.last_action_time:
                    last_action = agent.last_action_time.strftime('%H:%M:%S')
                
                # Get stack and status from latest calls if available
                if hasattr(agent, 'get_player_state'):
                    try:
                        # Use asyncio.run in a thread if needed
                        # For now, just check if there's cached state
                        if hasattr(agent, '_cached_player_state'):
                            cached_state = agent._cached_player_state
                            stack = cached_state.stack
                            status = cached_state.status.name
                    except:
                        pass
            except:
                pass
                
            self.active_agents_section[name] = {
                "address": address,
                "stack": stack,
                "status": status,
                "last_action": last_action
            }
    
    def _refresh_timer_stats(self):
        """Refresh timer agent statistics"""
        if not self.timer_agent:
            return
            
        active_timers = 0
        timer_details = []
        
        try:
            if hasattr(self.timer_agent, 'active_timers'):
                active_timers = len(self.timer_agent.active_timers)
                
                # Get details for each timer
                for player, expiry in self.timer_agent.active_timers.items():
                    short_addr = player[:8] + "..." + player[-6:]
                    time_left = (expiry - datetime.now()).total_seconds()
                    timer_details.append((short_addr, max(0, round(time_left, 1))))
        except:
            pass
            
        self.timer_section = {
            "active_count": active_timers,
            "timers": timer_details
        }
    
    def _draw_header(self, stdscr, row):
        """Draw the header section"""
        height, width = stdscr.getmaxyx()
        
        # Calculate runtime
        runtime = datetime.now() - self.start_time
        hours, remainder = divmod(runtime.total_seconds(), 3600)
        minutes, seconds = divmod(remainder, 60)
        runtime_str = f"{int(hours):02}:{int(minutes):02}:{int(seconds):02}"
        
        header = f"╔{'═' * (width-2)}╗"
        title = f"╠══ POKER AGENT MONITORING SYSTEM {'═' * (width-36)}╣"
        stats = f"╠══ Runtime: {runtime_str} | Hands: {self.hands_played} | Actions: {self.actions_taken} | Blinds: {self.current_blinds[0]}/{self.current_blinds[1]} {'═' * (width-75)}╣"
        divider = f"╠{'═' * (width-2)}╣"
        
        try:
            stdscr.addstr(row, 0, header)
            stdscr.addstr(row+1, 0, title)
            stdscr.addstr(row+2, 0, stats)
            stdscr.addstr(row+3, 0, divider)
        except curses.error:
            pass
            
        return row + 4
    
    def _draw_agent_section(self, stdscr, row):
        """Draw the active agents section"""
        height, width = stdscr.getmaxyx()
        
        self._refresh_agent_stats()
        
        try:
            stdscr.addstr(row, 0, f"╠══ ACTIVE AGENTS ({len(self.active_agents_section)}) {'═' * (width-25)}╣")
            row += 1
            
            if self.active_agents_section:
                headers = ["Name", "Address", "Stack", "Status", "Last Action"]
                table_data = []
                
                for name, data in self.active_agents_section.items():
                    table_data.append([
                        name,
                        data["address"],
                        data["stack"],
                        data["status"],
                        data["last_action"]
                    ])
                
                table = tabulate(table_data, headers=headers, tablefmt="simple")
                
                for i, line in enumerate(table.split('\n')):
                    if row + i >= height - 1:
                        break
                    padded_line = f"║ {line}{' ' * (width-len(line)-3)}║"
                    stdscr.addstr(row + i, 0, padded_line)
                
                row += len(table.split('\n'))
            else:
                stdscr.addstr(row, 0, f"║ No active agents{' ' * (width-19)}║")
                row += 1
            
            stdscr.addstr(row, 0, f"╠{'═' * (width-2)}╣")
            row += 1
        except curses.error:
            pass
        
        return row
    
    def _draw_timer_section(self, stdscr, row):
        """Draw the timer agent section"""
        height, width = stdscr.getmaxyx()
        
        self._refresh_timer_stats()
        
        try:
            stdscr.addstr(row, 0, f"╠══ TIMER AGENT {'═' * (width-16)}╣")
            row += 1
            
            active_count = self.timer_section.get("active_count", 0)
            timers = self.timer_section.get("timers", [])
            
            status = "Active" if self.timer_agent and hasattr(self.timer_agent, 'is_running') and self.timer_agent.is_running else "Inactive"
            
            stdscr.addstr(row, 0, f"║ Status: {status} | Active Timers: {active_count}{' ' * (width-26-len(status)-len(str(active_count)))}║")
            row += 1
            
            if timers:
                for i, (addr, time_left) in enumerate(timers):
                    if row >= height - 1:
                        break
                    stdscr.addstr(row, 0, f"║ Player: {addr} | Time Left: {time_left}s{' ' * (width-30-len(addr)-len(str(time_left)))}║")
                    row += 1
            
            stdscr.addstr(row, 0, f"╠{'═' * (width-2)}╣")
            row += 1
        except curses.error:
            pass
            
        return row
    
    def _draw_game_state(self, stdscr, row):
        """Draw the current game state section"""
        height, width = stdscr.getmaxyx()
        
        try:
            stdscr.addstr(row, 0, f"╠══ CURRENT GAME STATE {'═' * (width-24)}╣")
            row += 1
            
            round_name = self.game_state_section.get("round", "Unknown")
            pot = self.game_state_section.get("pot", 0)
            current_player = self.game_state_section.get("current_player", "None")
            if current_player != "None" and len(current_player) > 12:
                current_player = current_player[:8] + "..." + current_player[-6:]
            
            hand_start = self.game_state_section.get("hand_start_time", 0)
            hand_time = 0
            if hand_start > 0:
                hand_time = int(time.time() - hand_start)
            
            stdscr.addstr(row, 0, f"║ Round: {round_name} | Pot: {pot} | Current Player: {current_player} | Hand Time: {hand_time}s{' ' * (width-55-len(round_name)-len(str(pot))-len(current_player)-len(str(hand_time)))}║")
            row += 1
            
            stdscr.addstr(row, 0, f"╠{'═' * (width-2)}╣")
            row += 1
        except curses.error:
            pass
            
        return row
    
    def _draw_events(self, stdscr, row):
        """Draw recent events section"""
        height, width = stdscr.getmaxyx()
        
        try:
            stdscr.addstr(row, 0, f"╠══ RECENT EVENTS {'═' * (width-18)}╣")
            row += 1
            
            if self.active_events:
                for i, (time, event_type, details) in enumerate(self.active_events):
                    if row + i >= height - 1:
                        break
                    
                    # Truncate details if too long
                    if len(details) > width - 30:
                        details = details[:width-33] + "..."
                    
                    stdscr.addstr(row + i, 0, f"║ {time} | {event_type}: {details}{' ' * (width-len(time)-len(event_type)-len(details)-8)}║")
                
                row += len(self.active_events)
            else:
                stdscr.addstr(row, 0, f"║ No recent events{' ' * (width-19)}║")
                row += 1
            
            stdscr.addstr(row, 0, f"╠{'═' * (width-2)}╣")
            row += 1
        except curses.error:
            pass
            
        return row
    
    def _draw_transactions(self, stdscr, row):
        """Draw transaction history section"""
        height, width = stdscr.getmaxyx()
        
        try:
            stdscr.addstr(row, 0, f"╠══ TRANSACTION HISTORY {'═' * (width-25)}╣")
            row += 1
            
            if self.transaction_history:
                for i, (time, tx_hash, action, status, details) in enumerate(self.transaction_history):
                    if row + i >= height - 1:
                        break
                    
                    # Set color based on status
                    color = curses.A_NORMAL
                    if status.lower() == "success":
                        color = curses.color_pair(1)  # Green
                    elif status.lower() == "failed":
                        color = curses.color_pair(2)  # Red
                    elif status.lower() == "pending":
                        color = curses.color_pair(3)  # Yellow
                    
                    # Truncate details if too long
                    max_details_len = width - len(time) - len(tx_hash) - len(action) - len(status) - 12
                    if len(details) > max_details_len:
                        details = details[:max_details_len-3] + "..."
                    
                    line = f"║ {time} | {tx_hash} | {action} | {status} | {details}{' ' * (width-len(time)-len(tx_hash)-len(action)-len(status)-len(details)-14)}║"
                    stdscr.addstr(row + i, 0, line, color)
                
                row += len(self.transaction_history)
            else:
                stdscr.addstr(row, 0, f"║ No transactions{' ' * (width-18)}║")
                row += 1
            
            stdscr.addstr(row, 0, f"╠{'═' * (width-2)}╣")
            row += 1
        except curses.error:
            pass
            
        return row
    
    def _draw_logs(self, stdscr, row):
        """Draw log messages section"""
        height, width = stdscr.getmaxyx()
        
        try:
            stdscr.addstr(row, 0, f"╠══ LOG MESSAGES ({len(self.log_messages)}) {'═' * (width-25)}╣")
            row += 1
            
            if self.log_messages:
                # Print debugging info about log messages
                print(f"Drawing {len(self.log_messages)} log messages")
                
                for i, log_entry in enumerate(self.log_messages):
                    if row + i >= height - 1:
                        break
                    
                    # Ensure correct format and data types
                    try:
                        time, message, level = log_entry
                        
                        # Set color based on log level
                        color = curses.A_NORMAL
                        if level == LogLevel.ERROR:
                            color = curses.color_pair(2)  # Red
                        elif level == LogLevel.WARNING:
                            color = curses.color_pair(3)  # Yellow
                        elif level == LogLevel.SUCCESS:
                            color = curses.color_pair(1)  # Green
                        
                        # Ensure message is a string
                        if not isinstance(message, str):
                            message = str(message)
                        
                        # Truncate message if too long
                        if len(message) > width - 15:
                            message = message[:width-18] + "..."
                        
                        # Calculate padding to ensure full width
                        padding = max(0, width - len(time) - len(message) - 6)
                        line = f"║ {time} | {message}{' ' * padding}║"
                        
                        # Draw the line with the appropriate color
                        stdscr.addstr(row + i, 0, line, color)
                    except Exception as e:
                        # In case of any error with a specific log entry, display error instead
                        error_msg = f"Error displaying log: {str(e)}"
                        if len(error_msg) > width - 15:
                            error_msg = error_msg[:width-18] + "..."
                        stdscr.addstr(row + i, 0, f"║ ERROR | {error_msg}{' ' * (width-len(error_msg)-13)}║", curses.color_pair(2))
                
                row += len(self.log_messages)
            else:
                stdscr.addstr(row, 0, f"║ No log messages{' ' * (width-19)}║")
                row += 1
            
            stdscr.addstr(row, 0, f"╚{'═' * (width-2)}╝")
            row += 1
        except curses.error as e:
            print(f"Error drawing log section: {e}")
        except Exception as e:
            print(f"General error in _draw_logs: {e}")
            
        return row
    
    def _draw_screen(self, stdscr):
        """Draw the complete UI"""
        # Clear screen
        stdscr.clear()
        
        # Setup colors
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)  # Success
        curses.init_pair(2, curses.COLOR_RED, -1)    # Error
        curses.init_pair(3, curses.COLOR_YELLOW, -1) # Warning
        
        # Process all pending messages
        while not self.message_queue.empty():
            try:
                msg = self.message_queue.get_nowait()
                self._process_message(msg)
            except queue.Empty:
                break
        
        # Draw all sections
        row = 0
        row = self._draw_header(stdscr, row)
        row = self._draw_agent_section(stdscr, row)
        row = self._draw_timer_section(stdscr, row)
        row = self._draw_game_state(stdscr, row)
        row = self._draw_events(stdscr, row)
        row = self._draw_transactions(stdscr, row)
        row = self._draw_logs(stdscr, row)
        
        # Refresh screen
        stdscr.refresh()
    
    def _ui_thread(self, stdscr):
        """UI thread function"""
        # Hide cursor
        curses.curs_set(0)
        
        # Enable keypad mode
        stdscr.keypad(True)
        
        # Set non-blocking getch
        stdscr.nodelay(True)
        
        # Store screen reference
        self.screen = stdscr
        
        # Main UI loop
        while self.is_running:
            try:
                # Draw screen
                self._draw_screen(stdscr)
                
                # Check for key press (q to quit)
                key = stdscr.getch()
                if key == ord('q'):
                    self.is_running = False
                    break
                
                # Sleep to avoid high CPU usage
                time.sleep(0.1)
            except KeyboardInterrupt:
                break
            except Exception as e:
                try:
                    # Try to log the error
                    self.add_log(f"UI Error: {str(e)}", LogLevel.ERROR)
                    time.sleep(1)
                except:
                    pass
        
        # Cleanup
        curses.endwin()
    
    def start(self):
        """Start the terminal UI"""
        if self.is_running:
            return
            
        self.is_running = True
        self.start_time = datetime.now()
        
        # Start UI thread
        threading.Thread(target=lambda: curses.wrapper(self._ui_thread), daemon=True).start()
        
        # Give UI thread time to initialize
        time.sleep(0.5)
        
        # Add initial logs directly to the log_messages list
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_messages = [
            [timestamp, "Terminal UI started", LogLevel.SUCCESS],
            [timestamp, "Press 'q' to quit", LogLevel.INFO],
            [timestamp, "System ready", LogLevel.INFO],
            [timestamp, "Log messages will appear here", LogLevel.INFO],
            [timestamp, "This is a test log message", LogLevel.WARNING]
        ]
        
        # Also add through the normal channel for good measure
        self.add_log("Terminal UI started", LogLevel.SUCCESS)
        self.add_log("Press 'q' to quit", LogLevel.INFO)
        self.add_log("System ready", LogLevel.INFO)
    
    def stop(self):
        """Stop the terminal UI"""
        self.is_running = False
        self.add_log("Terminal UI stopping", LogLevel.WARNING)
        time.sleep(0.5)  # Give time for final messages
        
        # Force exit curses mode
        if self.screen:
            curses.endwin()

# Global UI instance for easy access
terminal_ui = TerminalUI()

# Monkeypatch the logger to capture all log messages
original_log = logging.Logger._log

def patched_log(self, level, msg, args, exc_info=None, extra=None, stack_info=False, stacklevel=1):
    """Patched logging method to capture logs to UI"""
    # Call original log method
    original_log(self, level, msg, args, exc_info, extra, stack_info, stacklevel)
    
    try:
        # Always add logs to terminal UI regardless of module (for debugging)
        # Format message
        formatted_msg = msg
        if args:
            try:
                formatted_msg = msg % args
            except Exception:
                formatted_msg = f"{msg} {str(args)}"
        
        # Add to UI based on level, with module name prefix for better context
        module_prefix = f"[{self.name}] " if not self.name.startswith('__main__') else ""
        
        # Convert log level to UI log level
        ui_level = LogLevel.INFO
        if level >= logging.ERROR:
            ui_level = LogLevel.ERROR
        elif level >= logging.WARNING:
            ui_level = LogLevel.WARNING
        
        # Force add to UI message queue directly for reliable delivery
        timestamp = datetime.now().strftime("%H:%M:%S")
        terminal_ui.message_queue.put((timestamp, f"{module_prefix}{formatted_msg}", ui_level))
    except Exception as e:
        # Last resort - direct print for debugging
        print(f"Error in patched_log: {e} - Original message: {msg}")

# Apply monkey patch if UI is the main interface
def enable_ui_logging():
    """Enable capturing logs to the terminal UI"""
    # Apply the monkey patch to capture logging
    logging.Logger._log = patched_log
    
    # Add some direct test logs to verify the UI is displaying logs correctly
    terminal_ui.add_log("Logging system initialized", LogLevel.SUCCESS)
    terminal_ui.add_log("UI Logging enabled", LogLevel.INFO)
    terminal_ui.add_log("Warning test message", LogLevel.WARNING)
    terminal_ui.add_log("Error test message", LogLevel.ERROR)
    
    # Also log through standard logging
    logging.info("Standard logging test message")
    logging.warning("Standard logging warning message")
    logging.error("Standard logging error message")
    
    # Print debug info
    print("UI Logging enabled - test messages added")

# Transaction monitoring
def monitor_transaction(tx_hash, action, status="Pending", details=""):
    """Add a transaction to the monitoring UI"""
    terminal_ui.add_transaction(tx_hash, action, status, details)

def update_transaction(tx_hash, status, details=""):
    """Update an existing transaction in the UI"""
    # Find the transaction in history and update it
    for tx in terminal_ui.transaction_history:
        if tx_hash in tx[1]:  # Check if hash matches
            tx[3] = status
            if details:
                tx[4] = details
            break

def register_game_event(event_type, details):
    """Register a game event in the UI"""
    terminal_ui.add_event(event_type, details)

def update_game_state(round_name, pot_size, current_player, blinds=(0, 0), hand_start_time=0):
    """Update the displayed game state"""
    terminal_ui.update_game_state(round_name, pot_size, current_player, blinds, hand_start_time)