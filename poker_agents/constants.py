from enum import IntEnum, Enum
from pathlib import Path
from dataclasses import dataclass
import os
import datetime

class BettingRound(IntEnum):
    PREFLOP = 0
    FLOP = 1
    TURN = 2
    RIVER = 3

class PlayerAction(Enum):
    FOLD = "FOLD"
    CHECK = "CHECK"
    CALL = "CALL"
    BET = "BET"
    RAISE = "RAISE"
    ALL_IN = "ALL IN"
    BLIND = "BLIND"
    TIMEOUT = "TIMEOUT"

# Find the project directory (where the script is running from)
PROJECT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

# Global session config that will be used by all agents
CONFIG_DIR = Path.home() / '.poker-agent'  # Keep this for backward compatibility
PROFILES_FILE = CONFIG_DIR / 'profiles.json'  # Keep profiles in the home directory
CONFIG_FILE = PROJECT_DIR / 'config.json'  # Look for config.json in the project directory
MIN_BALANCE_THRESHOLD = 0.1

# Transcript directory and settings
LOGS_DIR = PROJECT_DIR.parent / 'logs'
TRANSCRIPTS_DIR = LOGS_DIR / 'transcripts'
TRANSCRIPT_DATE_FORMAT = "%Y-%m-%d_%H-%M-%S"

# Create log directories if they don't exist
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)

# Function to generate a new transcript filename based on current time
def get_new_transcript_filename():
    timestamp = datetime.datetime.now().strftime(TRANSCRIPT_DATE_FORMAT)
    return f"poker_game_{timestamp}.txt"

# Default transcript filename
CURRENT_TRANSCRIPT_FILE = TRANSCRIPTS_DIR / get_new_transcript_filename()

@dataclass
class SessionConfig:
    router_address: str = None
    state_storage_address: str = None
    game_logic_address: str = None
    betting_contract_address: str = None

CURRENT_SESSION = SessionConfig()