// SPDX-License-Identifier: MIT
pragma solidity ^0.8.18;

import './interfaces.sol';
import './library.sol';

/**
 * @title Router
 * @dev Coordinates interactions between poker contracts and handles access control
 */
contract Router is IRouter {
    using PokerConstants for uint8;

    // Contract type constants
    uint8 private constant STATE_STORAGE = 0;
    uint8 private constant GAME_LOGIC = 1;
    uint8 private constant TOURNAMENT_LOGIC = 2;
    uint8 private constant HAND_MANAGER = 3;
    uint8 private constant HAND_EVALUATOR = 4;

    // Contract addresses
    address private immutable owner;
    address private stateStorage;
    address private gameLogic;
    address private tournamentLogic;
    address private handManager;
    address private handEvaluator;

    // Access control
    mapping(address => bool) private admins;
    mapping(address => bool) private whitelistedPlayers;
    mapping(address => bool) private authorizedTimers;

    // Action types
    uint8 private constant FOLD = 0;
    uint8 private constant CHECK = 1;
    uint8 private constant CALL = 2;
    uint8 private constant RAISE = 3;

    // Events
    event AdminAdded(address indexed admin);
    event AdminRemoved(address indexed admin);
    event PlayerWhitelisted(address indexed player);
    event PlayerBlacklisted(address indexed player);
    event PlayerEliminated(address indexed player);
    event HandStarted(uint256 timestamp, address firstToAct);
    event RoundAdvanced(uint8 newRound, uint256 timestamp);
    event DealerCardsDealt(uint8 cardType, uint256 timestamp); // 0=flop, 1=turn, 2=river
    event ShowdownInitiated(uint256 timestamp);
    event PotAwarded(address indexed winner, uint256 amount);
    event TournamentCompleted(address indexed winner, uint256 timestamp);

    event BlindLevelUpdated(
        uint256 indexed level,
        uint256 smallBlind,
        uint256 bigBlind,
        uint256 timestamp
    );

    event BlindUpdateScheduled(
        uint256 indexed currentLevel,
        uint256 nextUpdateTime
    );

    event TournamentStateUpdated(
        uint256 indexed blindLevel,
        uint8 remainingPlayers,
        uint256 averageStack
    );

    // Modifiers
    modifier onlyOwner() {
        require(msg.sender == owner, 'Only owner');
        _;
    }

    modifier onlyAdmin() {
        require(admins[msg.sender], 'Only admin');
        _;
    }

    modifier onlyWhitelisted() {
        require(whitelistedPlayers[msg.sender], 'Not whitelisted');
        _;
    }

    modifier onlyTimerBackend() {
        require(authorizedTimers[msg.sender], 'Not authorized timer');
        _;
    }

    modifier onlyGameLogicOrAdmin() {
        require(
            msg.sender == gameLogic || admins[msg.sender],
            'Only game logic or admin'
        );
        _;
    }

    // New modifier to validate it's the player's turn
    modifier onlyCurrentTurn() {
        IStateStorage.GameState memory gameState = IStateStorage(stateStorage)
            .getGameState();
        require(gameState.currentTurn == msg.sender, 'Not your turn');
        _;
    }

    constructor(
        address _stateStorage,
        address _gameLogic,
        address _tournamentLogic,
        address _handManager,
        address _handEvaluator
    ) {
        owner = msg.sender;
        admins[msg.sender] = true;

        stateStorage = _stateStorage;
        gameLogic = _gameLogic;
        tournamentLogic = _tournamentLogic;
        handManager = _handManager;
        handEvaluator = _handEvaluator;
    }

    // Poker betting contract address
    address private pokerBettingContract;
    
    /**
     * @notice Set the poker betting contract address
     * @param _pokerBettingContract Address of the betting contract
     */
    function setPokerBettingContract(address _pokerBettingContract) external onlyOwner {
        require(_pokerBettingContract != address(0), 'Invalid address');
        pokerBettingContract = _pokerBettingContract;
    }
    
    /**
     * @notice Start a new hand of poker
     * @return dealerSeed The seed used for shuffling
     */
    function startNewHand() external returns (bytes32) {
        // Allow GameLogic to start new hands automatically
        require(
            msg.sender == gameLogic || admins[msg.sender],
            'Only game logic or admin can start hands'
        );
        require(handManager != address(0), 'Hand manager not set');

        // Call startNewHand function on HandManager
        (bool success, bytes memory result) = handManager.call(
            abi.encodeWithSignature('startNewHand()')
        );
        require(success, 'Failed to start new hand');

        bytes32 dealerSeed = abi.decode(result, (bytes32));

        // Get the current turn player for the event
        IStateStorage.GameState memory gameState = IStateStorage(stateStorage)
            .getGameState();

        // Notify the betting contract that a hand has started to close betting window
        if (pokerBettingContract != address(0)) {
            // Get current tournament ID (assuming it's 1 since we don't track it explicitly)
            uint256 tournamentId = 1;
            (bool notifySuccess, ) = pokerBettingContract.call(
                abi.encodeWithSignature('handleHandStart(uint256)', tournamentId)
            );
            // We don't want to revert the hand start if the notification fails
            // Just continue with the hand
        }

        emit HandStarted(block.timestamp, gameState.currentTurn);
        return dealerSeed;
    }

    /**
     * @notice Advance to the next betting round
     */
    function nextRound() external onlyTimerBackend {
        require(gameLogic != address(0), 'Game logic not set');

        IStateStorage.GameState memory gameState = IStateStorage(stateStorage)
            .getGameState();
        require(
            gameState.currentRound < IStateStorage.BettingRound.River,
            'Already at final round'
        );

        // Call nextRound in GameLogic
        IGameLogic(gameLogic).nextRound();

        // Get updated state
        gameState = IStateStorage(stateStorage).getGameState();

        emit RoundAdvanced(uint8(gameState.currentRound), block.timestamp);
    }

    /**
     * @notice Deal the flop (first three community cards)
     */
    function dealFlop() external onlyTimerBackend {
        require(handManager != address(0), 'Hand manager not set');

        // Verify it's time for the flop
        IStateStorage.GameState memory gameState = IStateStorage(stateStorage)
            .getGameState();
        require(
            gameState.currentRound == IStateStorage.BettingRound.PreFlop,
            'Not time for flop'
        );
        require(isRoundComplete(), 'Betting round not complete');

        // Call the deal function
        (bool success, ) = handManager.call(
            abi.encodeWithSignature('dealFlop()')
        );
        require(success, 'Failed to deal flop');

        emit DealerCardsDealt(0, block.timestamp);

        // Automatically advance to next round
        IGameLogic(gameLogic).nextRound();
    }

    /**
     * @notice Deal the turn (fourth community card)
     */
    function dealTurn() external onlyTimerBackend {
        require(handManager != address(0), 'Hand manager not set');

        // Verify it's time for the turn
        IStateStorage.GameState memory gameState = IStateStorage(stateStorage)
            .getGameState();
        require(
            gameState.currentRound == IStateStorage.BettingRound.Flop,
            'Not time for turn'
        );
        require(isRoundComplete(), 'Betting round not complete');

        // Call the deal function
        (bool success, ) = handManager.call(
            abi.encodeWithSignature('dealTurn()')
        );
        require(success, 'Failed to deal turn');

        emit DealerCardsDealt(1, block.timestamp);

        // Automatically advance to next round
        IGameLogic(gameLogic).nextRound();
    }

    /**
     * @notice Deal the river (fifth community card)
     */
    function dealRiver() external onlyTimerBackend {
        require(handManager != address(0), 'Hand manager not set');

        // Verify it's time for the river
        IStateStorage.GameState memory gameState = IStateStorage(stateStorage)
            .getGameState();
        require(
            gameState.currentRound == IStateStorage.BettingRound.Turn,
            'Not time for river'
        );
        require(isRoundComplete(), 'Betting round not complete');

        // Call the deal function
        (bool success, ) = handManager.call(
            abi.encodeWithSignature('dealRiver()')
        );
        require(success, 'Failed to deal river');

        emit DealerCardsDealt(2, block.timestamp);

        // Automatically advance to next round
        IGameLogic(gameLogic).nextRound();
    }

    /**
     * @notice Process player elimination
     * @param player Address of the player to eliminate
     */
    function eliminatePlayer(address player) external returns (bool) {
        require(tournamentLogic != address(0), 'Tournament logic not set');
        require(
            msg.sender == gameLogic || admins[msg.sender],
            'Only game logic or admin can eliminate players'
        );

        IStateStorage.Player memory playerState = IStateStorage(stateStorage)
            .getPlayer(player);
        require(playerState.stack == 0, 'Player still has chips');

        // Call processElimination on TournamentLogic
        ITournamentLogic(tournamentLogic).processElimination(player);

        emit PlayerEliminated(player);
        return true;
    }

    /**
     * @notice Initiate showdown to determine hand winner
     */
    function initiateShowdown() external onlyTimerBackend {
        require(gameLogic != address(0), 'Game logic not set');

        IStateStorage.GameState memory gameState = IStateStorage(stateStorage)
            .getGameState();
        require(
            gameState.currentRound == IStateStorage.BettingRound.River,
            'Not ready for showdown'
        );
        require(isRoundComplete(), 'Betting round not complete');

        // In the current implementation, showdown is handled by GameLogic
        // This could call a specific showdown function if added to GameLogic

        emit ShowdownInitiated(block.timestamp);
    }

    /**
     * @notice Manual blind update route for timer backend
     * @dev This is a more reliable way to update blinds compared to automatic updates
     */
    function routeBlindUpdate() external override onlyTimerBackend {
        IStateStorage.TournamentState memory tournament = IStateStorage(
            stateStorage
        ).getTournamentState();

        require(
            tournament.tableState == IStateStorage.TableState.Active,
            'Tournament not active'
        );
        require(!tournament.isPaused, 'Tournament paused');

        // Call the more direct updateBlinds method to force a blind update
        // This method will reliably increment blinds if enough time has passed
        try ITournamentLogic(tournamentLogic).updateBlinds() {
            // Successfully updated blinds
        } catch {
            // Fallback to less restrictive method if needed
            try ITournamentLogic(tournamentLogic).checkAndUpdateBlinds() {
                // Successfully checked and possibly updated blinds
            } catch {
                // Log error but don't revert
            }
        }

        // Get updated tournament state
        tournament = IStateStorage(stateStorage).getTournamentState();

        // Calculate and emit average stack information
        uint256 averageStack = _calculateAverageStack();

        // Emit events for front-end tracking
        emit BlindLevelUpdated(
            tournament.currentBlindLevel,
            tournament.smallBlind,
            tournament.bigBlind,
            block.timestamp
        );

        emit TournamentStateUpdated(
            tournament.currentBlindLevel,
            tournament.activePlayerCount,
            averageStack
        );

        // Schedule next blind update
        uint256 nextUpdateTime = tournament.lastBlindUpdate +
            tournament.blindTimer;
        emit BlindUpdateScheduled(tournament.currentBlindLevel, nextUpdateTime);
    }

    // Helper function to calculate average stack
    function _calculateAverageStack() private view returns (uint256) {
        IStateStorage.TournamentState memory tournament = IStateStorage(
            stateStorage
        ).getTournamentState();

        if (tournament.activePlayerCount == 0) return 0;

        uint256 totalChips = 0;
        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address playerAddr = IStateStorage(stateStorage)
                .getPlayerAtPosition(i);
            if (playerAddr != address(0)) {
                IStateStorage.Player memory player = IStateStorage(stateStorage)
                    .getPlayer(playerAddr);
                if (player.status == IStateStorage.PlayerStatus.Active) {
                    totalChips += player.stack;
                }
            }
        }

        return totalChips / tournament.activePlayerCount;
    }

    /**
     * @notice Route game actions to appropriate contract
     * @param action Action type (0=Fold, 1=Check, 2=Call, 3=Raise)
     * @param data Encoded action data, can contain reasoning or amount+reasoning
     */
    function routeGameAction(
        uint8 action,
        bytes calldata data
    ) external override onlyWhitelisted onlyCurrentTurn {
        require(action <= RAISE, 'Invalid action');

        IStateStorage.TournamentState memory tournamentState = IStateStorage(
            stateStorage
        ).getTournamentState();

        require(
            tournamentState.tableState == IStateStorage.TableState.Active,
            'Tournament not active'
        );
        require(!tournamentState.isPaused, 'Tournament paused');

        uint256 amount = 0;
        string memory reasoning = "";
        
        // Extract data based on action type and data format
        if (action == RAISE) {
            // For RAISE we expect at least 32 bytes for amount
            if (data.length >= 32) {
                // First 32 bytes are the amount
                amount = abi.decode(data, (uint256));
                
                // If there's more data after the amount, it's the reasoning
                if (data.length > 32) {
                    // Extract reasoning from the remaining bytes (skip the first 32 bytes)
                    reasoning = string(data[32:]);
                }
            }
        } else {
            // For FOLD, CHECK, CALL - data contains only reasoning
            if (data.length > 0) {
                reasoning = string(data);
            }
        }

        // Process the action through GameLogic
        IGameLogic(gameLogic).processAction(msg.sender, action, amount);

        // Emit event for tracking with reasoning
        emit IRouter.PlayerAction(msg.sender, action, amount, reasoning);
    }

    /**
     * @notice Handle timeout actions from authorized backend
     * @param player The player who timed out
     */
    function routeTimeoutAction(
        address player
    ) external override onlyTimerBackend {
        IStateStorage.TournamentState memory tournamentState = IStateStorage(
            stateStorage
        ).getTournamentState();

        require(
            tournamentState.tableState == IStateStorage.TableState.Active,
            'Tournament not active'
        );
        require(!tournamentState.isPaused, 'Tournament paused');

        // Verify the player is currently the active turn
        IStateStorage.GameState memory gameState = IStateStorage(stateStorage)
            .getGameState();
        require(gameState.currentTurn == player, "Not player's turn");

        IGameLogic(gameLogic).handlePlayerTimeout(player);

        // Emit timeout action event with timeout reasoning
        emit IRouter.PlayerAction(player, FOLD, 0, "AI dozed off - timeout"); // Timeout is processed as a fold
    }

    /**
     * @notice Route tournament actions
     * @param selector Function selector
     * @param data Function data
     */
    function routeTournamentAction(
        bytes4 selector,
        bytes calldata data
    ) external override onlyAdmin {
        require(tournamentLogic != address(0), 'Tournament logic not set');

        (bool success, bytes memory result) = tournamentLogic.call(
            abi.encodePacked(selector, data)
        );
        require(success, string(result));
    }

    /**
     * @notice Upgrade contract implementation
     * @param contractType Type of contract to upgrade
     * @param implementation New implementation address
     */
    function upgradeContract(
        uint8 contractType,
        address implementation
    ) external override onlyOwner {
        require(implementation != address(0), 'Invalid implementation');

        if (contractType == STATE_STORAGE) {
            stateStorage = implementation;
        } else if (contractType == GAME_LOGIC) {
            gameLogic = implementation;
        } else if (contractType == TOURNAMENT_LOGIC) {
            tournamentLogic = implementation;
        } else if (contractType == HAND_MANAGER) {
            handManager = implementation;
        } else if (contractType == HAND_EVALUATOR) {
            handEvaluator = implementation;
        } else {
            revert('Invalid contract type');
        }

        emit ContractUpgraded(contractType, implementation);
    }

    // Utility function to check if current betting round is complete
    function isRoundComplete() public view returns (bool) {
        // This is a simplified check - the detailed implementation would
        // need to check if all active players have acted and betting is equal
        IStateStorage.GameState memory gameState = IStateStorage(stateStorage)
            .getGameState();
        return gameState.currentTurn == address(0);
    }

    // Timer Backend Management

    /**
     * @notice Add authorized timer backend
     * @param backend Address to authorize
     */
    function addTimerBackend(address backend) external onlyOwner {
        require(backend != address(0), 'Invalid address');
        require(!authorizedTimers[backend], 'Already authorized');
        authorizedTimers[backend] = true;
        emit TimerBackendAdded(backend);
    }

    /**
     * @notice Remove timer backend authorization
     * @param backend Address to deauthorize
     */
    function removeTimerBackend(address backend) external onlyOwner {
        require(authorizedTimers[backend], 'Not authorized');
        authorizedTimers[backend] = false;
        emit TimerBackendRemoved(backend);
    }

    // Admin Management

    /**
     * @notice Add new admin
     * @param admin Address to add as admin
     */
    function addAdmin(address admin) external onlyOwner {
        require(admin != address(0), 'Invalid address');
        require(!admins[admin], 'Already admin');
        admins[admin] = true;
        emit AdminAdded(admin);
    }

    /**
     * @notice Remove admin
     * @param admin Address to remove from admins
     */
    function removeAdmin(address admin) external onlyOwner {
        require(admin != owner, 'Cannot remove owner');
        require(admins[admin], 'Not admin');
        admins[admin] = false;
        emit AdminRemoved(admin);
    }

    /**
     * @notice Whitelist player for tournament
     * @param player Player address to whitelist
     */
    function whitelistPlayer(address player) external onlyAdmin {
        require(player != address(0), 'Invalid address');
        require(!whitelistedPlayers[player], 'Already whitelisted');
        whitelistedPlayers[player] = true;
        emit PlayerWhitelisted(player);
    }

    /**
     * @notice Remove player from whitelist
     * @param player Player address to remove
     */
    function blacklistPlayer(address player) external onlyAdmin {
        require(whitelistedPlayers[player], 'Not whitelisted');
        whitelistedPlayers[player] = false;
        emit PlayerBlacklisted(player);
    }

    // View functions for convenient state access

    /**
     * @notice Get current game state
     * @return currentTurn Current player's turn
     * @return currentRound Current betting round
     * @return currentBet Current bet amount
     * @return pot Main pot size
     * @return communityCards Array of community cards
     */
    function getCurrentGameState()
        external
        view
        returns (
            address currentTurn,
            uint8 currentRound,
            uint256 currentBet,
            uint256 pot,
            uint8[5] memory communityCards
        )
    {
        IStateStorage.GameState memory gameState = IStateStorage(stateStorage)
            .getGameState();

        return (
            gameState.currentTurn,
            uint8(gameState.currentRound),
            gameState.currentBet,
            gameState.mainPot,
            gameState.communityCards
        );
    }

    /**
     * @notice Get valid actions for a player
     * @param player Player address to check
     * @return validActions Array of booleans indicating valid actions [fold, check, call, raise]
     */
    function getValidActions(
        address player
    ) external view returns (bool[] memory) {
        require(gameLogic != address(0), 'Game logic not set');
        return IGameLogic(gameLogic).getValidActions(player);
    }

    /**
     * @notice Get current tournament state
     * @return smallBlind Current small blind
     * @return bigBlind Current big blind
     * @return activePlayerCount Number of active players
     * @return isPaused Whether tournament is paused
     * @return blindLevel Current blind level
     */
    function getCurrentTournamentState()
        external
        view
        returns (
            uint256 smallBlind,
            uint256 bigBlind,
            uint8 activePlayerCount,
            bool isPaused,
            uint256 blindLevel
        )
    {
        IStateStorage.TournamentState memory tournament = IStateStorage(
            stateStorage
        ).getTournamentState();

        return (
            tournament.smallBlind,
            tournament.bigBlind,
            tournament.activePlayerCount,
            tournament.isPaused,
            tournament.currentBlindLevel
        );
    }

    /**
     * @notice Check tournament completion status
     * @return isComplete Whether tournament is complete
     * @return winner Address of winner if tournament is complete
     */
    function isTournamentComplete()
        external
        view
        returns (bool isComplete, address winner)
    {
        require(tournamentLogic != address(0), 'Tournament logic not set');
        return ITournamentLogic(tournamentLogic).checkTournamentStatus();
    }

    // View Functions (existing ones)

    /**
     * @notice Check if address is admin
     * @param account Address to check
     * @return isAdmin True if address is admin
     */
    function isAdmin(address account) external view returns (bool) {
        return admins[account];
    }

    /**
     * @notice Check if player is whitelisted
     * @param player Address to check
     * @return isWhitelisted True if player is whitelisted
     */
    function isWhitelisted(address player) external view returns (bool) {
        return whitelistedPlayers[player];
    }

    /**
     * @notice Check if backend is authorized timer
     * @param backend Address to check
     * @return isAuthorized True if backend is authorized timer
     */
    function isAuthorizedTimer(address backend) external view returns (bool) {
        return authorizedTimers[backend];
    }

    /**
     * @notice Get contract address by type
     * @param contractType Type of contract
     * @return implementation Contract address
     */
    function getImplementation(
        uint8 contractType
    ) external view returns (address) {
        if (contractType == STATE_STORAGE) return stateStorage;
        if (contractType == GAME_LOGIC) return gameLogic;
        if (contractType == TOURNAMENT_LOGIC) return tournamentLogic;
        if (contractType == HAND_MANAGER) return handManager;
        if (contractType == HAND_EVALUATOR) return handEvaluator;
        revert('Invalid contract type');
    }

    // TODO: Ziga - remove other functions that also try to change dealer or blind positions
    function updateButtonPosition() external {
        ITournamentLogic(tournamentLogic).updateButtonPosition();
    }
    
    /**
     * @notice Deal the flop and advance to the next round
     * @dev Coordinates between HandManager and GameLogic for seamless round transition
     * @return flopCards The three flop cards
     */
    function dealFlopAndAdvance() external onlyTimerBackend returns (uint8[] memory flopCards) {
        // Deal flop cards first using direct contract reference
        // HandManager doesn't have a specific interface in interfaces.sol
        // Use the local handManager reference which is already stored
        
        // Use low-level call to avoid interface issues
        (bool success, bytes memory result) = handManager.call(
            abi.encodeWithSignature("dealFlop()")
        );
        
        require(success, "Deal flop failed");
        flopCards = abi.decode(result, (uint8[]));
        
        // Check if current turn is zero address and advance round if needed
        IStateStorage.GameState memory gameState = IStateStorage(stateStorage).getGameState();
        if (gameState.currentTurn == address(0)) {
            // Call nextRound to advance the game state
            IGameLogic(gameLogic).nextRound();
        }
        
        return flopCards;
    }
    
    /**
     * @notice Deal the turn and advance to the next round
     * @dev Coordinates between HandManager and GameLogic for seamless round transition
     * @return turnCard The turn card
     */
    function dealTurnAndAdvance() external onlyTimerBackend returns (uint8[] memory turnCard) {
        // Deal turn card first using direct contract reference
        // HandManager doesn't have a specific interface in interfaces.sol
        // Use the local handManager reference which is already stored
        
        // Use low-level call to avoid interface issues
        (bool success, bytes memory result) = handManager.call(
            abi.encodeWithSignature("dealTurn()")
        );
        
        require(success, "Deal turn failed");
        turnCard = abi.decode(result, (uint8[]));
        
        // Check if current turn is zero address and advance round if needed
        IStateStorage.GameState memory gameState = IStateStorage(stateStorage).getGameState();
        if (gameState.currentTurn == address(0)) {
            // Call nextRound to advance the game state
            IGameLogic(gameLogic).nextRound();
        }
        
        return turnCard;
    }
    
    /**
     * @notice Deal the river and advance to the next round
     * @dev Coordinates between HandManager and GameLogic for seamless round transition
     * @return riverCard The river card
     */
    function dealRiverAndAdvance() external onlyTimerBackend returns (uint8[] memory riverCard) {
        // Deal river card first using direct contract reference
        // HandManager doesn't have a specific interface in interfaces.sol
        // Use the local handManager reference which is already stored
        
        // Use low-level call to avoid interface issues
        (bool success, bytes memory result) = handManager.call(
            abi.encodeWithSignature("dealRiver()")
        );
        
        require(success, "Deal river failed");
        riverCard = abi.decode(result, (uint8[]));
        
        // Check if current turn is zero address and advance round if needed
        IStateStorage.GameState memory gameState = IStateStorage(stateStorage).getGameState();
        if (gameState.currentTurn == address(0)) {
            // Call nextRound to advance the game state
            IGameLogic(gameLogic).nextRound();
        }
        
        return riverCard;
    }
}