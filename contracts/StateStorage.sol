// SPDX-License-Identifier: MIT
pragma solidity ^0.8.18;

import './interfaces.sol';

contract StateStorage {
    // State storage
    mapping(address => Player) private players;
    mapping(uint8 => address) private positionToPlayer;
    mapping(address => bool) private hasActedInCurrentRound; // New: Track player actions
    TournamentState private tournamentState;
    GameState private gameState;
    BlindLevel[] private blindLevels;

    // Access control
    address private immutable owner;
    mapping(address => bool) private authorizedContracts;

    mapping(uint256 => SidePot) public sidePots;
    mapping(uint256 => mapping(address => bool)) public potEligibility;
    uint256 public sidePotCount;

    // Add new state variable for previous round's community cards
    uint8[5] private previousCommunityCards;

    event ContractAuthorized(address indexed contractAddress);
    event ContractDeauthorized(address indexed contractAddress);
    event BlindLevelAdded(
        uint256 indexed level,
        uint256 smallBlind,
        uint256 bigBlind
    );

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

    struct BlindLevel {
        uint256 smallBlind;
        uint256 bigBlind;
        uint256 startTime;
    }

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

    modifier onlyOwner() {
        require(msg.sender == owner, 'Only owner');
        _;
    }

    modifier onlyAuthorized() {
        require(authorizedContracts[msg.sender], 'Not authorized');
        _;
    }

    constructor() {
        owner = msg.sender;

        // Initialize tournament state
        tournamentState.smallBlind = 25;
        tournamentState.bigBlind = 50;
        tournamentState.blindTimer = 5 minutes;
        tournamentState.tableState = TableState.Waiting;
        tournamentState.activePlayerCount = 0;
        tournamentState.isPaused = false;
        tournamentState.currentBlindLevel = 0;

        // Initialize first blind level
        BlindLevel memory initialLevel = BlindLevel({
            smallBlind: 25,
            bigBlind: 50,
            startTime: block.timestamp
        });
        blindLevels.push(initialLevel);

        // Initialize game state
        gameState.actionTimer = 30 seconds;
        gameState.currentRound = BettingRound.PreFlop;
        gameState.mainPot = 0;
        gameState.currentBet = 0;
        gameState.lastRaise = 0;
    }

    // New functions for action tracking
    function hasPlayerActedInRound(
        address player
    ) external view returns (bool) {
        return hasActedInCurrentRound[player];
    }

    function setPlayerActedInRound(
        address player,
        bool acted
    ) external onlyAuthorized {
        hasActedInCurrentRound[player] = acted;
    }

    // Reset all player actions for new round
    function resetPlayerActions() external onlyAuthorized {
        for (uint8 i = 0; i < 9; i++) {
            // MAX_PLAYERS = 9
            address player = positionToPlayer[i];
            if (player != address(0)) {
                hasActedInCurrentRound[player] = false;
            }
        }
    }

    // Access control
    function authorizeContract(address contractAddress) external onlyOwner {
        require(contractAddress != address(0), 'Invalid address');
        authorizedContracts[contractAddress] = true;
        emit ContractAuthorized(contractAddress);
    }

    function deauthorizeContract(address contractAddress) external onlyOwner {
        require(contractAddress != address(0), 'Invalid address');
        authorizedContracts[contractAddress] = false;
        emit ContractDeauthorized(contractAddress);
    }

    // State getters
    function getPlayer(address player) external view returns (Player memory) {
        return players[player];
    }

    function getPlayerAtPosition(
        uint8 position
    ) external view returns (address) {
        return positionToPlayer[position];
    }

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
        )
    {
        return (
            tournamentState.smallBlind,
            tournamentState.bigBlind,
            tournamentState.blindTimer,
            tournamentState.lastBlindUpdate,
            uint8(tournamentState.tableState),
            tournamentState.buttonPosition,
            tournamentState.activePlayerCount,
            tournamentState.startTime,
            tournamentState.isPaused,
            tournamentState.currentBlindLevel
        );
    }

    function getTournamentState()
        external
        view
        returns (TournamentState memory)
    {
        return tournamentState;
    }

    function updateTournamentState(
        TournamentState memory newState
    ) external onlyAuthorized {
        tournamentState = newState;
    }

    function getGameState() external view returns (GameState memory) {
        return gameState;
    }

    function updatePlayerState(
        address player,
        Player memory newState
    ) external onlyAuthorized {
        players[player] = newState;
        if (newState.status == PlayerStatus.Eliminated) {
            positionToPlayer[newState.position] = address(0);
        } else {
            positionToPlayer[newState.position] = player;
        }
    }

    function updateTournamentBlinds(
        uint256 small,
        uint256 big
    ) external onlyAuthorized {
        tournamentState.smallBlind = small;
        tournamentState.bigBlind = big;
    }

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
        )
    {
        return (
            gameState.actionTimer,
            gameState.communityCards,
            uint8(gameState.currentRound),
            gameState.mainPot,
            gameState.currentBet,
            gameState.lastRaise,
            gameState.minRaise,
            gameState.lastAggressor,
            gameState.currentTurn,
            gameState.handStartTime,
            gameState.lastActionAmount
        );
    }

    function updateGameBasics(
        uint8 currentRound,
        uint256 mainPot,
        uint256 currentBet,
        address currentTurn
    ) external onlyAuthorized {
        gameState.currentRound = BettingRound(currentRound);
        gameState.mainPot = mainPot;
        gameState.currentBet = currentBet;
        gameState.currentTurn = currentTurn;
    }

    function updateGameCards(
        uint8[5] calldata communityCards
    ) external onlyAuthorized {
        gameState.communityCards = communityCards;
    }

    function updateGameTimers(
        uint256 actionTimer,
        uint256 handStartTime
    ) external onlyAuthorized {
        gameState.actionTimer = actionTimer;
        gameState.handStartTime = handStartTime;
    }

    function updateTournamentStatus(
        TableState newState,
        uint8 activeCount,
        bool isPaused
    ) external onlyAuthorized {
        tournamentState.tableState = newState;
        tournamentState.activePlayerCount = activeCount;
        tournamentState.isPaused = isPaused;
        if (newState == TableState.Active && tournamentState.startTime == 0) {
            tournamentState.startTime = block.timestamp;
        }
    }

    function updateTournamentPositions(uint8 button) external onlyAuthorized {
        tournamentState.buttonPosition = button;
    }

    function updateGameState(
        GameState memory newState
    ) external onlyAuthorized {
        // Store current community cards before updating
        if (gameState.currentRound == BettingRound.River) {
            previousCommunityCards = gameState.communityCards;
        }
        gameState = newState;
    }

    // Side pot functions
    function getSidePot(
        uint256 index
    ) external view returns (uint256 amount, bool isResolved) {
        return (sidePots[index].amount, sidePots[index].isResolved);
    }

    function createSidePot(
        uint256 index,
        uint256 amount
    ) external onlyAuthorized {
        sidePots[index] = SidePot(amount, false);
        sidePotCount++;
    }

    function setPotEligibility(
        uint256 potIndex,
        address player,
        bool eligible
    ) external onlyAuthorized {
        potEligibility[potIndex][player] = eligible;
    }

    function setSidePotResolved(uint256 index) external onlyAuthorized {
        sidePots[index].isResolved = true;
    }

    function isPlayerEligibleForPot(
        uint256 potIndex,
        address player
    ) external view returns (bool) {
        return potEligibility[potIndex][player];
    }

    // Utility getters
    function getCurrentBlinds() external view returns (uint256, uint256) {
        return (tournamentState.smallBlind, tournamentState.bigBlind);
    }

    // Blind Functions
    function getCurrentBlindLevel() external view returns (BlindLevel memory) {
        require(blindLevels.length > 0, 'No blind levels');
        return blindLevels[blindLevels.length - 1];
    }

    function getBlindHistory() external view returns (BlindLevel[] memory) {
        return blindLevels;
    }

    function addBlindLevel(BlindLevel memory newLevel) external onlyAuthorized {
        require(newLevel.startTime >= block.timestamp, 'Invalid start time');
        require(
            newLevel.smallBlind > 0 && newLevel.bigBlind > 0,
            'Invalid blind values'
        );

        blindLevels.push(newLevel);
        tournamentState.currentBlindLevel = blindLevels.length - 1;
        tournamentState.smallBlind = newLevel.smallBlind;
        tournamentState.bigBlind = newLevel.bigBlind;
        tournamentState.lastBlindUpdate = newLevel.startTime;

        emit BlindLevelAdded(
            tournamentState.currentBlindLevel,
            newLevel.smallBlind,
            newLevel.bigBlind
        );
    }

    function getTournamentStateArray()
        external
        view
        returns (
            uint256[] memory values,
            uint8[] memory smallValues,
            bool isPaused
        )
    {
        uint256[] memory vals = new uint256[](7);
        vals[0] = tournamentState.smallBlind;
        vals[1] = tournamentState.bigBlind;
        vals[2] = tournamentState.blindTimer;
        vals[3] = tournamentState.lastBlindUpdate;
        vals[4] = tournamentState.startTime;
        vals[5] = tournamentState.currentBlindLevel;

        uint8[] memory svals = new uint8[](3);
        svals[0] = uint8(tournamentState.tableState);
        svals[1] = tournamentState.buttonPosition;
        svals[2] = tournamentState.activePlayerCount;

        return (vals, svals, tournamentState.isPaused);
    }

    // Add new getter for previous community cards
    function getPreviousCommunityCards()
        external
        view
        returns (uint8[5] memory)
    {
        return previousCommunityCards;
    }

    // Add a function to explicitly set previous community cards
    function setPreviousCommunityCards(
        uint8[5] memory cards
    ) external onlyAuthorized {
        previousCommunityCards = cards;
    }
}
