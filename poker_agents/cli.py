import questionary
import asyncio
import json
from pathlib import Path
import logging
from getpass import getpass
import os
from typing import Dict
from tabulate import tabulate
from datetime import datetime

from .timer_agent import TimerAgent
from typing import Dict, Optional, List
import signal
import argparse

from .agent import PokerAgent
from .constants import CONFIG_DIR, PROFILES_FILE, CONFIG_FILE, CURRENT_SESSION, SessionConfig
from .openrouter_client import OpenRouterClient
from .config_manager import ConfigManager
# Import the new terminal UI
from .terminal_ui import terminal_ui, enable_ui_logging, register_game_event
from .terminal_ui import update_game_state, monitor_transaction, update_transaction, LogLevel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ProfileManager:
    def __init__(self):
        self.ensure_config_dir()

    def ensure_config_dir(self):
        """Ensure configuration directory exists"""
        CONFIG_DIR.mkdir(exist_ok=True)

    def load_profiles(self) -> dict:
        """Load existing agent profiles"""
        if PROFILES_FILE.exists():
            with open(PROFILES_FILE, 'r') as f:
                return json.load(f)
        return {'profiles': {}}

    def save_profiles(self, profiles: dict):
        """Save agent profiles"""
        with open(PROFILES_FILE, 'w') as f:
            json.dump(profiles, f, indent=2)

    def create_profile(self, name: str, rpc_url: str, private_key: str, 
                      model_name: str) -> bool:
        """Create a new agent profile"""
        profiles = self.load_profiles()
        
        if name in profiles['profiles']:
            return False

        profiles['profiles'][name] = {
            'id': str(datetime.now().timestamp()),
            'rpc_url': rpc_url,
            'private_key': private_key,  # Note: In production, encrypt this
            'model_name': model_name,
            'created_at': datetime.now().isoformat(),
            'last_used': None
        }
        
        self.save_profiles(profiles)
        return True

    def get_profile(self, name: str) -> dict:
        """Get a specific profile"""
        profiles = self.load_profiles()
        return profiles['profiles'].get(name)

    def update_last_used(self, name: str):
        """Update last used timestamp"""
        profiles = self.load_profiles()
        if name in profiles['profiles']:
            profiles['profiles'][name]['last_used'] = datetime.now().isoformat()
            self.save_profiles(profiles)

class PokerAgentCLI:
    def __init__(self):
        try:
            self.profile_manager = ProfileManager()
            self.active_agents: Dict[str, PokerAgent] = {}
            self.timer_agent: Optional[TimerAgent] = None
            self.running = True
            self.config_manager = ConfigManager(CONFIG_FILE)
            self.use_ui = True  # Default to using the UI
            self._load_config()
        except Exception as e:
            logger.error(f"Failed to initialize CLI: {e}")
            raise

    def _load_config(self):
        print(f"Looking for config file at: {CONFIG_FILE.absolute()}")
        config = self.config_manager.load_config()
        if contract_config := config['contracts']:
            CURRENT_SESSION.router_address = contract_config['router']
            CURRENT_SESSION.state_storage_address = contract_config['state_storage']
            CURRENT_SESSION.game_logic_address = contract_config['game_logic']

    async def start_from_config(self, config: dict):
        """Start agents and timer from configuration file in non-interactive mode"""
        try:
            # Initialize terminal UI if enabled
            if self.use_ui:
                terminal_ui.start()
                terminal_ui.add_log("Starting in non-interactive mode using config.json...", level=LogLevel.INFO)
                enable_ui_logging()  # Enable capturing logs to UI
            else:
                print("\nStarting in non-interactive mode using config.json...")
            
            # 1. Load and validate contract addresses
            if not (contracts := await self._load_contract_addresses(config)):
                if self.use_ui:
                    terminal_ui.add_log("Failed to load contract addresses. Check configuration.", level=LogLevel.ERROR)
                else:
                    print("❌ Failed to load contract addresses. Check configuration.")
                return False

            success = True  # Track overall startup success
            running_components = []  # Track what was started successfully

            # 2. Start timer agent if configured
            if timer_config := config.get('timer_agent'):
                if await self._start_timer_agent(timer_config):
                    running_components.append("Timer Agent")
                    if self.use_ui:
                        terminal_ui.set_timer_agent(self.timer_agent)  # Register timer agent with UI
                else:
                    if self.use_ui:
                        terminal_ui.add_log("Failed to start timer agent, but continuing with poker agents...", level=LogLevel.WARNING)
                    else:
                        print("⚠️ Failed to start timer agent, but continuing with poker agents...")

            # 3. Start configured poker agents
            if agents_config := config.get('agents'):
                if self.use_ui:
                    terminal_ui.add_log(f"Found agents in config: {agents_config.keys()}", level=LogLevel.INFO)
                else:
                    print(f"Found agents in config: {agents_config.keys()}")
                started_agents = await self._start_configured_agents(agents_config)
                if started_agents:
                    running_components.extend(started_agents)
                    # Register agents with UI
                    if self.use_ui:
                        terminal_ui.set_agents(self.active_agents)
                else:
                    if self.use_ui:
                        terminal_ui.add_log("No agents were started successfully.", level=LogLevel.WARNING)
                    else:
                        print("⚠️ No agents were started successfully.")
            else:
                if self.use_ui:
                    terminal_ui.add_log("No agents configuration found in config.json", level=LogLevel.WARNING)
                else:
                    print("⚠️ No agents configuration found in config.json")

            # 4. Status summary
            if not running_components:
                if self.use_ui:
                    terminal_ui.add_log("No components were started successfully.", level=LogLevel.ERROR)
                else:
                    print("❌ No components were started successfully.")
                return False

            if self.use_ui:
                terminal_ui.add_log("Startup Complete!", level=LogLevel.SUCCESS)
                for component in running_components:
                    terminal_ui.add_log(f"Running: {component}", level=LogLevel.INFO)
                register_game_event("STARTUP", "Poker agents system started successfully")
            else:
                print("\n✅ Startup Complete!")
                print("Running components:")
                for component in running_components:
                    print(f"  • {component}")
            
            # 5. Keep system running with status updates and periodic refresh
            while self.running:
                if self.active_agents or self.timer_agent:
                    try:
                        # Update game state in UI if possible
                        if self.timer_agent and self.use_ui:
                            try:
                                game_state = await self.timer_agent.get_game_state()
                                if game_state:
                                    # Get round name
                                    round_names = ["PREFLOP", "FLOP", "TURN", "RIVER"]
                                    current_round = round_names[game_state[2]] if 0 <= game_state[2] < len(round_names) else "UNKNOWN"
                                    
                                    # Get pot size and current player
                                    pot_size = game_state[3]
                                    current_player = game_state[8]
                                    hand_start_time = game_state[9]
                                    
                                    # Get current blinds
                                    tournament_state = await self.timer_agent.get_tournament_state()
                                    if tournament_state and len(tournament_state) >= 2:
                                        current_blinds = (tournament_state[0], tournament_state[1])
                                    else:
                                        current_blinds = (0, 0)
                                        
                                    # Update UI
                                    update_game_state(current_round, pot_size, current_player, current_blinds, hand_start_time)
                            except Exception as e:
                                logger.error(f"Error updating game state in UI: {e}")
                    except Exception as e:
                        logger.error(f"Error in status update: {e}")
                        
                    await asyncio.sleep(2)  # Shorter polling for more responsive UI
                else:
                    await asyncio.sleep(1)

            return True

        except Exception as e:
            logger.error(f"Error in non-interactive startup: {e}")
            await self.cleanup()
            return False
        finally:
            # Make sure to stop the UI when done
            if self.use_ui and terminal_ui.is_running:
                terminal_ui.stop()

    async def _load_contract_addresses(self, config: dict) -> Optional[dict]:
        """Load and validate contract addresses from config"""
        if not (contracts := config.get('contracts')):
            print("Missing 'contracts' section in config!")
            return None

        print("\nDebugging contract addresses:")
        print(f"Config contracts: {contracts}")
        
        # Resolve environment variables for each contract address
        resolved_contracts = {}
        for key, value in contracts.items():
            if isinstance(value, str):
                # Check if the value is an environment variable name
                env_value = os.getenv(value)
                print(f"Looking up env var '{value}': {env_value}")
                if env_value:
                    # If found in environment, use that value
                    resolved_contracts[key] = env_value
                    print(f"Resolved {key} address from env var {value} to {env_value}")
                else:
                    # Keep the original value if not an environment variable
                    resolved_contracts[key] = value
                    print(f"Using {key} address directly from config: {value}")
            else:
                resolved_contracts[key] = value

        print(f"Resolved contracts: {resolved_contracts}")

        required_contracts = ['router', 'state_storage', 'game_logic']
        missing = [c for c in required_contracts if not resolved_contracts.get(c)]
        
        if missing:
            print("Missing required contract addresses:")
            for addr in missing:
                print(f"  • {addr}")
            return None

        # Set addresses in current session
        CURRENT_SESSION.router_address = resolved_contracts['router']
        CURRENT_SESSION.state_storage_address = resolved_contracts['state_storage']
        CURRENT_SESSION.game_logic_address = resolved_contracts['game_logic']
        
        # Set betting contract address if provided
        if 'betting_contract' in resolved_contracts:
            CURRENT_SESSION.betting_contract_address = resolved_contracts['betting_contract']
            print(f"Betting contract address: {CURRENT_SESSION.betting_contract_address}")

        print("\n✅ Contract addresses loaded:")
        for name, addr in resolved_contracts.items():
            print(f"  • {name}: {addr}")

        return resolved_contracts

    async def _start_timer_agent(self, timer_config: dict) -> bool:
        """Start timer agent from config"""
        print("\nStarting Timer Agent...")
        
        if not all([timer_config.get('rpc_url'), timer_config.get('private_key')]):
            print("❌ Missing timer agent configuration (rpc_url or private_key)")
            return False

        try:
            timer_agent = TimerAgent()
            success = await timer_agent.initialize(
                rpc_url=timer_config['rpc_url'],
                private_key=timer_config['private_key'],
                router_address=CURRENT_SESSION.router_address,
                game_logic_address=CURRENT_SESSION.game_logic_address,
                state_storage_address=CURRENT_SESSION.state_storage_address,
                betting_contract_address=CURRENT_SESSION.betting_contract_address
            )

            if success:
                self.timer_agent = timer_agent
                asyncio.create_task(timer_agent.start())
                print("✅ Timer Agent started successfully")
                return True
            else:
                print("❌ Failed to initialize Timer Agent")
                return False

        except Exception as e:
            logger.error(f"Timer agent startup error: {e}")
            print(f"❌ Error starting Timer Agent: {e}")
            return False

    async def _start_configured_agents(self, agents_config: dict) -> List[str]:
        """Start poker agents from config"""
        print("\nStarting configured poker agents...")
        started_agents = []

        for name, agent_config in agents_config.items():
            try:
                print(f"\nStarting Agent: {name}")
                
                # Get values directly from config (with fallbacks)
                rpc_url = agent_config.get('rpc_url')
                private_key = agent_config.get('private_key')
                model = agent_config.get('model', "anthropic/claude-2")

                if not all([rpc_url, private_key]):
                    print(f"❌ Missing configuration for agent {name}")
                    continue

                # Initialize and start agent
                agent = PokerAgent(model_name=model)
                success = await agent.initialize(
                    rpc_url=rpc_url,
                    private_key=private_key,
                    router_address=CURRENT_SESSION.router_address,
                    state_storage_address=CURRENT_SESSION.state_storage_address,
                    game_logic_address=CURRENT_SESSION.game_logic_address
                )

                if success:
                    self.active_agents[name] = agent
                    task = asyncio.create_task(self._run_agent(name, name, agent))
                    task.add_done_callback(
                        lambda t: self._handle_agent_completion(t, name, name)
                    )
                    print(f"✅ Agent {name} started successfully")
                    started_agents.append(f"Agent: {name}")
                else:
                    print(f"❌ Failed to initialize agent {name}")

            except Exception as e:
                logger.error(f"Error starting agent {name}: {e}")
                print(f"❌ Error starting agent {name}: {e}")

        return started_agents

    async def _print_system_status(self):
        """Print current system status"""
        print("\n=== System Status ===")
        if self.timer_agent:
            print(f"Timer Agent: Running (Active timers: {len(self.timer_agent.active_timers)})")
        print(f"Active Agents: {len(self.active_agents)}")
        for name, agent in self.active_agents.items():
            last_action = "Never"
            if agent.last_action_time:
                last_action = agent.last_action_time.strftime('%H:%M:%S')
            print(f"  • {name} (Last action: {last_action})")
        print("===================")

    async def setup_timer_agent(self):
        """Set up and start the timer agent"""
        if self.timer_agent and self.timer_agent.is_running:
            print("Timer agent is already running!")
            return

        print("\nSet up timer agent")
        
        rpc_url = await questionary.text("Enter RPC URL:").ask_async()
        private_key = await questionary.password(
            "Enter timer backend private key (must be authorized):"
        ).ask_async()

        timer_agent = TimerAgent()
        try:
            success = await timer_agent.initialize(
                rpc_url=rpc_url,
                private_key=private_key,
                router_address=CURRENT_SESSION.router_address,
                game_logic_address=CURRENT_SESSION.game_logic_address,
                state_storage_address=CURRENT_SESSION.state_storage_address,
                betting_contract_address=CURRENT_SESSION.betting_contract_address
            )

            if success:
                self.timer_agent = timer_agent
                # Create task for timer agent
                asyncio.create_task(timer_agent.start())
                print("Timer agent started successfully!")
            else:
                print("Failed to initialize timer agent!")

        except Exception as e:
            print(f"Error starting timer agent: {e}")

    async def stop_timer_agent(self):
        """Stop the timer agent"""
        if not self.timer_agent or not self.timer_agent.is_running:
            print("No timer agent is running!")
            return

        await self.timer_agent.stop()
        print("Timer agent stopped successfully!")

    async def view_timer_status(self):
        """View timer agent status"""
        if not self.timer_agent:
            print("\nTimer agent not initialized")
            return

        print("\nTimer Agent Status:")
        print(f"Running: {self.timer_agent.is_running}")
        print(f"Address: {self.timer_agent.account.address if self.timer_agent.account else 'Not set'}")
        print("\nActive Timers:")
        for player, expiry in self.timer_agent.active_timers.items():
            time_left = expiry - datetime.now()
            print(f"Player: {player}")
            print(f"Time remaining: {time_left.total_seconds():.1f}s")

        input("\nPress Enter to continue...")

    async def cleanup(self):
        """Cleanup all agents on exit"""
        if self.active_agents:
            print("\nStopping all poker agents...")
            await self.stop_all_agents()
        
        if self.timer_agent and self.timer_agent.is_running:
            print("Stopping timer agent...")
            await self.timer_agent.stop()

    async def main_menu(self):
        """Main menu loop with error handling"""
        try:
            # Start terminal UI if enabled
            if self.use_ui:
                terminal_ui.start()
                enable_ui_logging()  # Enable capturing logs to UI
            
            # First, set up contract addresses for the session
            await self.setup_contract_addresses()

            while self.running:
                choice = await questionary.select(
                    "Poker Agent Manager - Main Menu",
                    choices=[
                        "1. Manage Profiles",
                        "2. Start Agents",
                        "3. Stop Agents",
                        "4. View Status",
                        "5. Update Contract Addresses",
                        "6. Manage Timer Agent",  # Added this option
                        "7. Exit"
                    ]
                ).ask_async()

                try:
                    if "1." in choice:
                        await self.profile_management_menu()
                    elif "2." in choice:
                        await self.start_agents_menu()
                    elif "3." in choice:
                        await self.stop_agents_menu()
                    elif "4." in choice:
                        await self.view_status()
                    elif "5." in choice:
                        await self.setup_contract_addresses()
                    elif "6." in choice:  # Added this handler
                        await self.timer_agent_menu()
                    else:
                        if await self.confirm_exit():
                            break

                except Exception as e:
                    logger.error(f"Error in menu operation: {e}")
                    print(f"\nError occurred: {e}")
                    if not await questionary.confirm("Continue to main menu?").ask_async():
                        break

        except KeyboardInterrupt:
            print("\nReceived exit signal...")
        finally:
            self.running = False
            await self.cleanup()

    async def timer_agent_menu(self):
        """Menu for managing timer agent"""
        while True:
            status = "Running" if (self.timer_agent and self.timer_agent.is_running) else "Stopped"
            choice = await questionary.select(
                f"Timer Agent Management (Status: {status})",
                choices=[
                    "1. Start Timer Agent",
                    "2. Stop Timer Agent",
                    "3. View Timer Agent Status",
                    "4. Back to Main Menu"
                ]
            ).ask_async()

            if "1." in choice:
                await self.setup_timer_agent()
            elif "2." in choice:
                await self.stop_timer_agent()
            elif "3." in choice:
                await self.view_timer_status()
            else:
                break

    async def setup_contract_addresses(self):
        """Set up contract addresses for the session"""
        print("\nSet up contract addresses for this session")
        print("(These will be used for all agents until you exit)")
        
        CURRENT_SESSION.router_address = await questionary.text(
            "Enter Router contract address:",
            default=CURRENT_SESSION.router_address or ""
        ).ask_async()
        
        CURRENT_SESSION.state_storage_address = await questionary.text(
            "Enter StateStorage contract address:",
            default=CURRENT_SESSION.state_storage_address or ""
        ).ask_async()

        CURRENT_SESSION.game_logic_address = await questionary.text(
            "Enter GameLogic contract address:",
            default=CURRENT_SESSION.game_logic_address or ""
        ).ask_async()
        
        CURRENT_SESSION.betting_contract_address = await questionary.text(
            "Enter Betting Contract address (optional):",
            default=CURRENT_SESSION.betting_contract_address or ""
        ).ask_async()

    async def profile_management_menu(self):
        """Profile management menu"""
        while True:
            choice = await questionary.select(
                "Profile Management",
                choices=[
                    "1. Create New Profile",
                    "2. View Profiles",
                    "3. Delete Profile",
                    "4. Back to Main Menu"
                ]
            ).ask_async()

            if "1." in choice:
                await self.create_profile()
            elif "2." in choice:
                await self.view_profiles()
            elif "3." in choice:
                await self.delete_profile()
            else:
                break

    async def create_profile(self):
        """Create new agent profile"""
        try:
            if not os.getenv("OPENROUTER_API_KEY"):
                print("OpenRouter API key not found! Please set OPENROUTER_API_KEY environment variable.")
                return

            # Get profile name first to check env vars
            name = await questionary.text("Enter profile name:").ask_async()
            if self.profile_manager.get_profile(name):
                print(f"Profile '{name}' already exists!")
                return

            # Generate env var names
            env_prefix = f"POKER_AGENT_{name.upper()}"
            rpc_var = f"{env_prefix}_RPC"
            key_var = f"{env_prefix}_KEY"
            model_var = f"{env_prefix}_MODEL"

            # Get model - either from env or user selection
            model_name = os.getenv(model_var)
            if not model_name:
                print("Fetching available models from OpenRouter...")
                client = OpenRouterClient()
                models = client.get_available_models()
                
                if not models:
                    model_name = "anthropic/claude-2"
                else:
                    model_choices = [f"{model['id']} ({model['context_length']} tokens)" for model in models]
                    choice = await questionary.select("Select model to use:", choices=model_choices).ask_async()
                    model_name = choice.split(" (")[0]

            # Get RPC and key - from env or user input
            rpc_url = os.getenv(rpc_var) or await questionary.text("Enter RPC URL:").ask_async()
            private_key = os.getenv(key_var) or await questionary.password("Enter private key:").ask_async()

            success = self.profile_manager.create_profile(
                name=name,
                rpc_url=rpc_url,
                private_key=private_key,
                model_name=model_name
            )

            if success:
                self.config_manager.save_agent_config(name, rpc_var, key_var, model_var)
                print(f"Profile '{name}' created successfully!")
                print(f"Environment variables for future use:")
                print(f"RPC URL: {rpc_var}")
                print(f"Private Key: {key_var}")
                print(f"Model: {model_var}")
            else:
                print(f"Failed to create profile '{name}'")

        except Exception as e:
            print(f"Error creating profile: {e}")
            logger.error(f"Profile creation error: {e}")

    async def view_profiles(self):
        """Display all profiles"""
        profiles = self.profile_manager.load_profiles()['profiles']
        
        if not profiles:
            print("No profiles found.")
            return

        headers = ['Name', 'Model', 'RPC URL', 'Created', 'Last Used', 'Status']
        rows = []

        for name, profile in profiles.items():
            status = 'Running' if profile['id'] in self.active_agents else 'Stopped'
            rows.append([
                name,
                profile['model_name'],
                profile['rpc_url'],
                profile['created_at'][:10],
                profile['last_used'][:10] if profile['last_used'] else 'Never',
                status
            ])

        print("\nAgent Profiles:")
        print(tabulate(rows, headers=headers, tablefmt='grid'))
        input("\nPress Enter to continue...")

    async def delete_profile(self):
        """Delete a profile"""
        profiles = self.profile_manager.load_profiles()['profiles']
        
        if not profiles:
            print("No profiles to delete!")
            return

        name = await questionary.select(
            "Select profile to delete:",
            choices=list(profiles.keys()) + ["Cancel"]
        ).ask_async()

        if name == "Cancel":
            return

        if profiles[name]['id'] in self.active_agents:
            print(f"Cannot delete profile '{name}' while agent is running!")
            return

        # Delete profile
        profiles.pop(name)
        self.profile_manager.save_profiles({'profiles': profiles})
        print(f"Profile '{name}' deleted successfully!")

    async def start_agents_menu(self):
        """Menu for starting agents"""
        profiles = self.profile_manager.load_profiles()['profiles']
        if not profiles:
            print("No profiles found. Please create a profile first.")
            return

        choices = [f"Start {name}" for name in profiles.keys()]
        choices.append("Start Multiple Agents")
        choices.append("Back")

        choice = await questionary.select(
            "Select agent to start:",
            choices=choices
        ).ask_async()

        if choice == "Back":
            return
        elif choice == "Start Multiple Agents":
            await self.start_multiple_agents()
        else:
            name = choice.replace("Start ", "")
            await self.start_single_agent(name)

    async def start_single_agent(self, name: str):
        """Start a single agent"""
        try:
            # Check contract addresses
            if not all([
                CURRENT_SESSION.router_address,
                CURRENT_SESSION.state_storage_address,
                CURRENT_SESSION.game_logic_address
            ]):
                print("Contract addresses not set! Please update them first.")
                return

            # Get and validate profile
            profile = self.profile_manager.get_profile(name)
            if not profile:
                print(f"Profile '{name}' not found!")
                return

            if profile['id'] in self.active_agents:
                print(f"Agent '{name}' is already running!")
                return

            print(f"Starting agent '{name}'...")
            
            agent = PokerAgent(model_name=profile['model_name'])
            try:
                success = await agent.initialize(
                    rpc_url=profile['rpc_url'],
                    private_key=profile['private_key'],
                    router_address=CURRENT_SESSION.router_address,
                    state_storage_address=CURRENT_SESSION.state_storage_address,
                    game_logic_address=CURRENT_SESSION.game_logic_address
                )

                if success:
                    self.active_agents[profile['id']] = agent
                    self.profile_manager.update_last_used(name)
                    task = asyncio.create_task(self._run_agent(name, profile['id'], agent))
                    # Add error callback
                    task.add_done_callback(lambda t: self._handle_agent_completion(t, name, profile['id']))
                    print(f"Agent '{name}' started successfully!")
                else:
                    print(f"Failed to initialize agent '{name}'")

            except Exception as e:
                print(f"Error starting agent '{name}': {e}")
                logger.error(f"Agent start error: {e}", exc_info=True)
                if agent in self.active_agents.values():
                    await agent.stop()
                    if profile['id'] in self.active_agents:
                        del self.active_agents[profile['id']]

        except Exception as e:
            print(f"Unexpected error starting agent '{name}': {e}")
            logger.error(f"Unexpected agent start error: {e}", exc_info=True)
            return False

    def _handle_agent_completion(self, task, name: str, profile_id: str):
        """Handle agent task completion"""
        try:
            task.result()  # Will raise exception if task failed
        except asyncio.CancelledError:
            logger.info(f"Agent '{name}' was cancelled")
        except Exception as e:
            logger.error(f"Agent '{name}' failed with error: {e}")
        finally:
            if profile_id in self.active_agents:
                del self.active_agents[profile_id]
            print(f"Agent '{name}' stopped")

    async def _run_agent(self, name: str, profile_id: str, agent: PokerAgent):
        """Run agent and handle cleanup on completion"""
        try:
            await agent.start()
        except Exception as e:
            logger.error(f"Agent '{name}' error: {e}", exc_info=True)
            print(f"Agent '{name}' encountered an error: {e}")
            raise  # Re-raise to trigger error handling
        finally:
            if profile_id in self.active_agents:
                del self.active_agents[profile_id]
            print(f"Agent '{name}' stopped")

    async def start_multiple_agents(self):
        """Start multiple selected agents"""
        profiles = self.profile_manager.load_profiles()['profiles']
        available_agents = [
            name for name, profile in profiles.items()
            if profile['id'] not in self.active_agents
        ]

        if not available_agents:
            print("No available agents to start!")
            return

        selected = await questionary.checkbox(
            "Select agents to start:",
            choices=available_agents
        ).ask_async()

        if not selected:
            return

        for name in selected:
            await self.start_single_agent(name)

    async def stop_agents_menu(self):
        """Menu for stopping agents"""
        if not self.active_agents:
            print("No agents are currently running.")
            return

        profiles = self.profile_manager.load_profiles()['profiles']
        choices = [
            f"Stop {name}" for name, profile in profiles.items()
            if profile['id'] in self.active_agents
        ]
        choices.extend(["Stop All Agents", "Back"])

        choice = await questionary.select(
            "Select agent to stop:",
            choices=choices
        ).ask_async()

        if choice == "Back":
            return
        elif choice == "Stop All Agents":
            await self.stop_all_agents()
        else:
            name = choice.replace("Stop ", "")
            await self.stop_single_agent(name)

    async def stop_single_agent(self, name: str):
        """Stop a single agent"""
        profile = self.profile_manager.get_profile(name)
        if not profile or profile['id'] not in self.active_agents:
            print(f"Agent '{name}' is not running!")
            return

        await self.active_agents[profile['id']].stop()
        del self.active_agents[profile['id']]
        print(f"Agent '{name}' stopped successfully!")

    async def stop_all_agents(self):
        """Stop all running agents"""
        if not self.active_agents:
            print("No agents are currently running!")
            return

        for agent in self.active_agents.values():
            await agent.stop()
        
        self.active_agents.clear()
        print("All agents stopped successfully!")

    async def view_status(self):
        """View status of all agents"""
        profiles = self.profile_manager.load_profiles()['profiles']
        
        headers = ['Name', 'Status', 'Model', 'Last Action']
        rows = []

        for name, profile in profiles.items():
            status = 'Running' if profile['id'] in self.active_agents else 'Stopped'
            last_action = 'Never'
            if status == 'Running':
                agent = self.active_agents[profile['id']]
                if agent.last_action_time:
                    last_action = agent.last_action_time.strftime('%H:%M:%S')

            rows.append([
                name,
                status,
                profile['model_name'],
                last_action
            ])

        print("\nAgent Status:")
        print(tabulate(rows, headers=headers, tablefmt='grid'))
        input("\nPress Enter to continue...")

    async def confirm_exit(self):
        """Confirm exit if agents are running"""
        if self.active_agents:
            return await questionary.confirm(
                "There are still agents running. Are you sure you want to exit?"
            ).ask_async()
        return True

async def main(interactive: bool = False, use_ui: bool = True, return_cli: bool = False):
    """Main entry point for the application.
    
    Args:
        interactive: Whether to run in interactive mode
        use_ui: Whether to use the terminal UI
        return_cli: If True, returns the CLI instance and cleanup task for external management
        
    Returns:
        If return_cli is True, returns (cli_instance, cleanup_task)
    """
    cli = PokerAgentCLI()
    cli.use_ui = use_ui  # Store UI preference in CLI instance
    cleanup_task = None
    
    # Create a specific cleanup task that can be awaited
    async def cleanup_handler():
        try:
            logger.info("Running cleanup handler...")
            # Create a copy of active agents to avoid modification during iteration
            if cli.active_agents:
                logger.info(f"Stopping {len(cli.active_agents)} active agents")
                await cli.stop_all_agents()
            
            if cli.timer_agent and cli.timer_agent.is_running:
                logger.info("Stopping timer agent")
                await cli.timer_agent.stop()
                
            if use_ui and 'terminal_ui' in globals() and terminal_ui.is_running:
                logger.info("Stopping terminal UI")
                terminal_ui.stop()
                
            logger.info("Cleanup complete")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
            
    # Set up cancellation handler
    if return_cli:
        cleanup_task = asyncio.create_task(asyncio.sleep(0))  # Dummy task by default
    
    try:
        if interactive:
            await cli.main_menu()
        else:
            config = cli.config_manager.load_config()
            if not config.get('contracts'):
                print("No configuration found. Run with --interactive to set up.")
                if return_cli:
                    return cli, cleanup_task
                return
            await cli.start_from_config(config)
            
        # After normal completion, run cleanup
        if not return_cli:
            await cleanup_handler()
        else:
            # Return a real cleanup task
            cleanup_task = asyncio.create_task(cleanup_handler())
            
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt detected in main")
        if not return_cli:
            await cleanup_handler()
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        if not return_cli:
            await cleanup_handler()
    
    # Return CLI instance if requested
    if return_cli:
        return cli, cleanup_task

if __name__ == "__main__":
    asyncio.run(main())