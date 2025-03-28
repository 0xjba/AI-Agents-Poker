// SPDX-License-Identifier: MIT
pragma solidity ^0.8.18;

interface IStateStorage {
    enum PlayerStatus {
        Inactive,
        Active,
        Folded,
        Eliminated,
        AllIn
    }
    enum TableState {
        Waiting,
        Active,
        Complete
    }
    enum BettingRound {
        PreFlop,
        Flop,
        Turn,
        River
    }

    struct BlindLevel {
        uint256 smallBlind;
        uint256 bigBlind;
        uint256 startTime;
    }

    struct Player {
        uint256 stack;
        PlayerStatus status;
        uint256 currentBet;
        uint8 position;
        uint8[2] holeCards;
        uint256 lastActionTime;
        uint256 totalContribution;
        uint8 playerIndex;
    }

    struct TournamentState {
        uint256 smallBlind;
        uint256 bigBlind;
        uint256 blindTimer;
        uint256 lastBlindUpdate;
        TableState tableState;
        uint8 buttonPosition;
        uint8 activePlayerCount;
        uint256 startTime;
        bool isPaused;
        uint256 currentBlindLevel;
    }

    struct GameState {
        uint256 actionTimer;
        uint8[5] communityCards;
        BettingRound currentRound;
        uint256 mainPot;
        uint256 currentBet;
        uint256 lastRaise;
        uint256 minRaise;
        uint8 lastAggressor;
        address currentTurn;
        uint256 handStartTime;
        uint256 lastActionAmount;
    }

    struct SidePot {
        uint256 amount;
        bool isResolved;
    }

    // New functions for player action tracking
    function hasPlayerActedInRound(address player) external view returns (bool);
    function setPlayerActedInRound(address player, bool acted) external;
    function resetPlayerActions() external;

    // Core State Functions
    function getPlayer(address player) external view returns (Player memory);
    function getTournamentState()
        external
        view
        returns (TournamentState memory);
    function getGameState() external view returns (GameState memory);

    // Game Progress Functions
    function advanceRound() external;
    function updateBlinds() external;
    function rotateDealerButton() external;
    function pauseTournament() external;
    function resumeTournament() external;
    function eliminatePlayer(address player) external;

    // State Update Functions
    function updatePlayerState(address player, Player memory newState) external;
    function updateTournamentState(TournamentState memory newState) external;
    function updateGameState(GameState memory newState) external;

    // Card Visibility Functions
    function getHoleCards(
        address player
    ) external view returns (uint8[2] memory);
    function revealFoldedHand(address player) external;
    function showdownReveal(address player) external;
    function getBestHand(address player) external view returns (uint256);

    // Required Functions
    function registerPlayer(address player) external;
    function getCurrentBlinds() external view returns (uint256, uint256);
    function getPlayerAtPosition(
        uint8 position
    ) external view returns (address);

    // Admin Functions
    function whitelistPlayer(address player) external;

    // SidePot
    function sidePotCount() external view returns (uint256);
    function createSidePot(uint256 index, uint256 amount) external;
    function setPotEligibility(
        uint256 potIndex,
        address player,
        bool eligible
    ) external;
    function getSidePot(
        uint256 index
    ) external view returns (uint256 amount, bool isResolved);
    function setSidePotResolved(uint256 index) external;
    function isPlayerEligibleForPot(
        uint256 potIndex,
        address player
    ) external view returns (bool);

    // Blind management
    function getCurrentBlindLevel() external view returns (BlindLevel memory);
    function getBlindHistory() external view returns (BlindLevel[] memory);
    function addBlindLevel(BlindLevel memory newLevel) external;

    function getTournamentStateValues()
        external
        view
        returns (
            uint256 smallBlind,
            uint256 bigBlind,
            uint256 blindTimer,
            uint256 lastBlindUpdate,
            uint8 tableState,
            uint8 buttonPosition,
            uint8 activePlayerCount,
            uint256 startTime,
            bool isPaused,
            uint256 currentBlindLevel
        );

    function getGameStateValues()
        external
        view
        returns (
            uint256 actionTimer,
            uint8[5] memory communityCards,
            uint8 currentRound,
            uint256 mainPot,
            uint256 currentBet,
            uint256 lastRaise,
            uint256 minRaise,
            uint8 lastAggressor,
            address currentTurn,
            uint256 handStartTime,
            uint256 lastActionAmount
        );

    function updateGameBasics(
        uint8 currentRound,
        uint256 mainPot,
        uint256 currentBet,
        address currentTurn
    ) external;

    function updateGameCards(uint8[5] calldata communityCards) external;

    function updateGameTimers(
        uint256 actionTimer,
        uint256 handStartTime
    ) external;

    function getTournamentStateArray()
        external
        view
        returns (
            uint256[] memory values,
            uint8[] memory smallValues,
            bool isPaused
        );

    function updateTournamentBlinds(uint256 small, uint256 big) external;

    function updateTournamentStatus(
        TableState newState,
        uint8 activeCount,
        bool isPaused
    ) external;

    function updateTournamentPositions(uint8 button) external;

    // Add the new functions for previous community cards
    function getPreviousCommunityCards()
        external
        view
        returns (uint8[5] memory);
    function setPreviousCommunityCards(uint8[5] memory cards) external;
}

interface IGameLogic {
    event ActionTaken(address indexed player, uint8 action, uint256 amount);
    event RoundStarted(IStateStorage.BettingRound round);
    event PlayerTimedOut(address indexed player);
    event ActionTimerStarted(
        address indexed player,
        uint256 duration,
        uint256 blockNumber
    );
    event RoundComplete(IStateStorage.BettingRound round);

    function processAction(
        address player,
        uint8 action,
        uint256 amount
    ) external;
    function nextRound() external;
    function handlePlayerTimeout(address player) external;
    function getValidActions(
        address player
    ) external view returns (bool[] memory validActions);
}

interface ITournamentLogic {
    event TournamentStarted(uint256 startTime);
    event BlindsIncreased(uint256 smallBlind, uint256 bigBlind);
    event PlayerEliminated(address indexed player);
    event TournamentCompleted(address indexed winner);
    event FirstHandStarted(uint256 timestamp);

    function startTournament(address[] calldata players) external;
    function updateBlinds() external;
    function checkAndUpdateBlinds() external;
    function processElimination(address player) external;
    function checkTournamentStatus()
        external
        view
        returns (bool isComplete, address winner);
    function getTournamentProgress()
        external
        view
        returns (
            uint256 elapsedTime,
            uint256 blindLevel,
            uint8 remainingPlayers
        );
    function updateButtonPosition() external;
    function recordFirstHandStart() external;
    
    // Debug functions - remove in production
    function debugGetFirstHandStartTime() external view returns (uint256);
    function debugGetBlindLevelCalculation() external;
    function getCurrentBlindLevel() external view returns (uint256);
}

interface IRouter {
    // Events
    event ContractUpgraded(uint8 indexed contractType, address implementation);
    event TimerBackendAdded(address indexed backend);
    event TimerBackendRemoved(address indexed backend);
    
    // Updated PlayerAction event to include reasoning
    event PlayerAction(
        address player,
        uint8 actionType,
        uint256 amount,
        string reasoning
    );
    
    // Core functions
    function routeGameAction(uint8 action, bytes calldata data) external;
    function routeTournamentAction(
        bytes4 selector,
        bytes calldata data
    ) external;
    function routeTimeoutAction(address player) external;
    function routeBlindUpdate() external;
    function upgradeContract(
        uint8 contractType,
        address implementation
    ) external;

    function eliminatePlayer(address player) external returns (bool);
    function startNewHand() external returns (bytes32);
    function updateButtonPosition() external;
    
    // Access control functions
    function isAdmin(address account) external view returns (bool);
    function isAuthorizedTimer(address backend) external view returns (bool);
}

/**
 * @title IPokerBettingContract
 * @dev Interface for the PokerBettingContract
 */
interface IPokerBettingContract {
    enum BettingState { Closed, Open, Settled }
    
    /**
     * @notice Handle tournament start event
     * @param players Array of player addresses
     */
    function handleTournamentStart(address[] calldata players) external;
    
    /**
     * @notice Handle new hand start - close betting
     * @param tournamentId Tournament ID
     */
    function handleHandStart(uint256 tournamentId) external;
    
    /**
     * @notice Handle player elimination event
     * @param tournamentId Tournament ID
     * @param player Eliminated player address
     */
    function handlePlayerElimination(uint256 tournamentId, address player) external;
    
    /**
     * @notice Handle tournament completion event
     * @param tournamentId Tournament ID
     * @param winner Address of tournament winner
     */
    function handleTournamentComplete(uint256 tournamentId, address winner) external;
    
    /**
     * @notice Continue distributing winnings in batches
     * @param tournamentId Tournament ID
     * @param batchNumber Batch number to process
     * @dev Only authorized timer backend can call this function
     */
    function continueDistributingWinnings(uint256 tournamentId, uint256 batchNumber) external;
    
    /**
     * @notice Check if all winnings have been distributed
     * @param tournamentId Tournament ID
     * @return isComplete Whether distribution is complete
     * @return totalBatches Total number of batches
     * @return totalBettors Total number of bettors who bet on the winner
     */
    function isDistributionComplete(uint256 tournamentId) external view returns (bool isComplete, uint256 totalBatches, uint256 totalBettors);
}
