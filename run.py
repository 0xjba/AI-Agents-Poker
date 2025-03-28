import asyncio
import sys
import argparse
import signal
import logging
from poker_agents.cli import main

# Set up logging
log_handler = logging.StreamHandler()
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

# Get the root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(log_handler)

# Add direct console logs for critical debugging information
print("===== Poker Agents System Starting =====")
print("Debug logs enabled")
logging.info("Application initialized")
logging.warning("Test warning message")
logging.error("Test error message")
logger = logging.getLogger(__name__)

# Store task references
main_task = None
cli_instance = None

# Signal handler for cleaner shutdown
def handle_shutdown(sig, frame):
    logger.info("Shutdown signal received. Cleaning up...")
    if main_task and not main_task.done():
        main_task.cancel()

# Main wrapper with better exception handling
async def main_wrapper(interactive=False, use_ui=False):
    try:
        # Pass a reference holder for cleanup
        global cli_instance
        cli_instance, cleanup_task = await main(interactive, use_ui, True)
        # Keep running until cleanup completes
        if cleanup_task:
            await cleanup_task
    except asyncio.CancelledError:
        logger.info("Main task was cancelled. Performing cleanup...")
        if cli_instance:
            try:
                await cli_instance.cleanup()
                logger.info("Cleanup completed successfully")
            except Exception as e:
                logger.error(f"Error during cleanup: {e}")
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received. Exiting gracefully...")
        if cli_instance:
            try:
                await cli_instance.cleanup()
                logger.info("Cleanup completed successfully")
            except Exception as e:
                logger.error(f"Error during cleanup: {e}")
    except Exception as e:
        logger.error(f"Unhandled exception: {e}", exc_info=True)
        if cli_instance:
            try:
                await cli_instance.cleanup()
            except:
                pass
    finally:
        logger.info("Exiting application")

if __name__ == "__main__":
    # Register signal handlers
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Run Poker Agents system")
    parser.add_argument("--interactive", action="store_true", help="Run in interactive mode")
    parser.add_argument("--no-ui", action="store_true", help="Disable terminal UI")
    
    args = parser.parse_args()
    
    # Run with terminal UI by default, unless --no-ui is specified
    use_ui = not args.no_ui
    
    # Create and store the main task
    loop = asyncio.get_event_loop()
    main_task = loop.create_task(main_wrapper(args.interactive, use_ui))
    
    try:
        # Run until complete
        loop.run_until_complete(main_task)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt at top level. Forcing exit.")
        # Force cancel task if needed
        if not main_task.done():
            main_task.cancel()
            try:
                loop.run_until_complete(main_task)
            except (asyncio.CancelledError, KeyboardInterrupt):
                pass
    finally:
        loop.close()