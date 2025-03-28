// SPDX-License-Identifier: MIT
pragma solidity ^0.8.18;

import "./interfaces.sol";

/**
 * @title PokerBettingContract
 * @dev Allows spectators to bet on poker tournament outcomes with fixed 5:1 odds
 */
contract PokerBettingContract is IPokerBettingContract {
    // State variables
    address public immutable router;
    address public immutable stateStorage;
    address public tournamentLogic;
    
    struct Tournament {
        uint256 id;
        BettingState state;
        address winner;
        uint256 totalPool;
        uint256 startTime;
        uint8 playerCount;
        mapping(address => bool) isActive;
        mapping(address => uint256) playerBetAmount;
        // Track all bettors who placed bets on each player
        mapping(address => address[]) playerBettors;
        // Track all unique bettors
        address[] allBettors;
        mapping(address => bool) isBettor;
    }
    
    struct PlayerBet {
        address player;
        uint256 amount;
        bool paid;
    }
    
    // Tournament tracking
    uint256 public currentTournamentId;
    mapping(uint256 => Tournament) public tournaments;
    mapping(uint256 => mapping(address => mapping(address => PlayerBet))) public bets;
    
    // Constants
    uint256 private constant FIXED_ODDS = 500; // 5:1 = 500%
    uint8 private constant MAX_PLAYERS = 5;
    uint256 private constant MAX_PAYOUT_BATCH_SIZE = 50; // Process payouts in batches
    
    // Events
    event TournamentBettingOpened(uint256 indexed tournamentId, uint8 playerCount);
    event BetPlaced(uint256 indexed tournamentId, address indexed bettor, address indexed player, uint256 amount);
    event TournamentSettled(uint256 indexed tournamentId, address winner);
    event WinningsDistributed(uint256 indexed tournamentId, address indexed bettor, uint256 amount);
    event HandStarted(uint256 indexed tournamentId);
    event BettingClosed(uint256 indexed tournamentId);
    event PayoutBatchProcessed(uint256 indexed tournamentId, uint256 batchNumber, uint256 processedCount);
    event AdditionalPayoutsNeeded(uint256 indexed tournamentId, uint256 remainingBatches, uint256 totalBettors);
    
    // Modifiers
    modifier onlyRouterOrAdmin() {
        require(
            msg.sender == router || 
            IRouter(router).isAdmin(msg.sender) || 
            (tournamentLogic != address(0) && msg.sender == tournamentLogic), 
            "Only router, admin, or tournament logic"
        );
        _;
    }
    
    modifier onlyTimerBackend() {
        require(IRouter(router).isAuthorizedTimer(msg.sender), "Only authorized timer backend");
        _;
    }
    
    modifier tournamentExists(uint256 tournamentId) {
        require(tournamentId > 0 && tournamentId <= currentTournamentId, "Tournament does not exist");
        _;
    }
    
    modifier bettingOpen(uint256 tournamentId) {
        require(tournaments[tournamentId].state == BettingState.Open, "Betting not open");
        _;
    }
    
    /**
     * @notice Initialize the betting contract
     * @param _router Router contract address
     * @param _stateStorage StateStorage contract address
     */
    constructor(address _router, address _stateStorage) {
        router = _router;
        stateStorage = _stateStorage;
        currentTournamentId = 0;
    }
    
    /**
     * @notice Set the tournament logic contract address
     * @param _tournamentLogic Address of the tournament logic contract
     */
    function setTournamentLogic(address _tournamentLogic) external {
        require(msg.sender == router || IRouter(router).isAdmin(msg.sender), "Only router or admin");
        require(_tournamentLogic != address(0), "Invalid address");
        tournamentLogic = _tournamentLogic;
    }
    
    /**
     * @notice Handle tournament start event from Router
     * @param players Array of player addresses
     */
    function handleTournamentStart(address[] calldata players) external override onlyRouterOrAdmin {
        require(players.length >= 2 && players.length <= MAX_PLAYERS, "Invalid player count");
        
        // Create new tournament
        currentTournamentId++;
        uint256 tournamentId = currentTournamentId;
        
        // Initialize tournament
        Tournament storage tournament = tournaments[tournamentId];
        tournament.id = tournamentId;
        tournament.state = BettingState.Open;
        tournament.startTime = block.timestamp;
        tournament.playerCount = uint8(players.length);
        
        // Mark players as active
        for (uint i = 0; i < players.length; i++) {
            tournament.isActive[players[i]] = true;
        }
        
        emit TournamentBettingOpened(tournamentId, uint8(players.length));
    }
    
    /**
     * @notice Handle new hand start - close betting
     * @param tournamentId Tournament ID
     */
    function handleHandStart(uint256 tournamentId) external override onlyRouterOrAdmin tournamentExists(tournamentId) {
        Tournament storage tournament = tournaments[tournamentId];
        
        // Only close betting if it's open
        if (tournament.state == BettingState.Open) {
            tournament.state = BettingState.Closed;
            emit BettingClosed(tournamentId);
        }
        
        emit HandStarted(tournamentId);
    }
    
    /**
     * @notice Place a bet on a player to win the tournament
     * @param tournamentId Tournament ID
     * @param player Player address to bet on
     */
    function placeBet(uint256 tournamentId, address player) 
        external 
        payable
        tournamentExists(tournamentId)
        bettingOpen(tournamentId)
    {
        Tournament storage tournament = tournaments[tournamentId];
        require(tournament.isActive[player], "Player not active in tournament");
        require(msg.value > 0, "Bet amount must be greater than 0");
        
        // Record the bet
        PlayerBet storage bet = bets[tournamentId][msg.sender][player];
        
        // If this is a new bet
        if (bet.amount == 0) {
            bet.player = player;
            bet.amount = msg.value;
            bet.paid = false;
            
            // Track this bettor for the player
            tournament.playerBettors[player].push(msg.sender);
            
            // Add to all bettors list if not already there
            if (!tournament.isBettor[msg.sender]) {
                tournament.allBettors.push(msg.sender);
                tournament.isBettor[msg.sender] = true;
            }
        } else {
            // If adding to existing bet
            bet.amount += msg.value;
        }
        
        // Update tournament state
        tournament.totalPool += msg.value;
        tournament.playerBetAmount[player] += msg.value;
        
        emit BetPlaced(tournamentId, msg.sender, player, msg.value);
    }
    
    /**
     * @notice Handle player elimination event from Router
     * @param tournamentId Tournament ID
     * @param player Eliminated player address
     */
    function handlePlayerElimination(uint256 tournamentId, address player) 
        external 
        override
        onlyRouterOrAdmin
        tournamentExists(tournamentId)
    {
        Tournament storage tournament = tournaments[tournamentId];
        require(tournament.state != BettingState.Settled, "Tournament already settled");
        
        // Mark player as inactive
        tournament.isActive[player] = false;
    }
    
    /**
     * @notice Handle tournament completion event from Router
     * @param tournamentId Tournament ID
     * @param winner Address of tournament winner
     */
    function handleTournamentComplete(uint256 tournamentId, address winner) 
        external 
        override
        onlyRouterOrAdmin
        tournamentExists(tournamentId)
    {
        Tournament storage tournament = tournaments[tournamentId];
        require(tournament.state != BettingState.Settled, "Tournament already settled");
        
        // Set winner and mark as settled
        tournament.winner = winner;
        tournament.state = BettingState.Settled;
        
        // Process first batch of winnings
        _distributeBatch(tournamentId, winner, 0);
        
        emit TournamentSettled(tournamentId, winner);
        
        // Check if there are more batches to process
        address[] storage winnerBettors = tournament.playerBettors[winner];
        if (winnerBettors.length > MAX_PAYOUT_BATCH_SIZE) {
            uint256 remainingBatches = (winnerBettors.length + MAX_PAYOUT_BATCH_SIZE - 1) / MAX_PAYOUT_BATCH_SIZE - 1;
            emit AdditionalPayoutsNeeded(tournamentId, remainingBatches, winnerBettors.length);
        }
    }
    
    /**
     * @notice Continue distributing winnings in batches
     * @param tournamentId Tournament ID
     * @param batchNumber Batch number to process
     */
    function continueDistributingWinnings(uint256 tournamentId, uint256 batchNumber) 
        external 
        onlyTimerBackend
        tournamentExists(tournamentId)
    {
        Tournament storage tournament = tournaments[tournamentId];
        require(tournament.state == BettingState.Settled, "Tournament not settled yet");
        require(tournament.winner != address(0), "No winner set");
        
        _distributeBatch(tournamentId, tournament.winner, batchNumber);
    }
    
    /**
     * @notice Distribute winnings to a batch of bettors
     * @param tournamentId Tournament ID
     * @param winner Winning player address
     * @param batchNumber Batch number to process
     */
    function _distributeBatch(uint256 tournamentId, address winner, uint256 batchNumber) internal {
        Tournament storage tournament = tournaments[tournamentId];
        
        // Process bettors who bet on the winner in batches
        address[] storage winnerBettors = tournament.playerBettors[winner];
        
        uint256 startIndex = batchNumber * MAX_PAYOUT_BATCH_SIZE;
        uint256 endIndex = startIndex + MAX_PAYOUT_BATCH_SIZE;
        
        // Ensure we don't go out of bounds
        if (startIndex >= winnerBettors.length) {
            return; // No more bettors to process
        }
        
        if (endIndex > winnerBettors.length) {
            endIndex = winnerBettors.length;
        }
        
        // Process this batch
        for (uint256 i = startIndex; i < endIndex; i++) {
            address bettor = winnerBettors[i];
            PlayerBet storage bet = bets[tournamentId][bettor][winner];
            
            if (bet.amount > 0 && !bet.paid) {
                // Calculate winnings with fixed 5:1 odds
                uint256 winnings = bet.amount + (bet.amount * FIXED_ODDS / 100);
                
                // Mark as paid
                bet.paid = true;
                
                // Transfer winnings to the bettor
                (bool success, ) = bettor.call{value: winnings}("");
                if (success) {
                    emit WinningsDistributed(tournamentId, bettor, winnings);
                }
            }
        }
        
        emit PayoutBatchProcessed(tournamentId, batchNumber, endIndex - startIndex);
        
        // Check if there are more batches to process after this one
        if (endIndex < winnerBettors.length) {
            uint256 remainingBatches = (winnerBettors.length - endIndex + MAX_PAYOUT_BATCH_SIZE - 1) / MAX_PAYOUT_BATCH_SIZE;
            emit AdditionalPayoutsNeeded(tournamentId, remainingBatches, winnerBettors.length - endIndex);
        }
    }
    
    /**
     * @notice Check if all winnings have been distributed
     * @param tournamentId Tournament ID
     */
    function isDistributionComplete(uint256 tournamentId) 
        external 
        view 
        tournamentExists(tournamentId) 
        returns (bool, uint256, uint256) 
    {
        Tournament storage tournament = tournaments[tournamentId];
        if (tournament.state != BettingState.Settled || tournament.winner == address(0)) {
            return (false, 0, 0);
        }
        
        address[] storage winnerBettors = tournament.playerBettors[tournament.winner];
        uint256 totalBatches = (winnerBettors.length + MAX_PAYOUT_BATCH_SIZE - 1) / MAX_PAYOUT_BATCH_SIZE;
        
        // Check if we've processed all batches
        return (winnerBettors.length == 0 || 
                totalBatches * MAX_PAYOUT_BATCH_SIZE >= winnerBettors.length, 
                totalBatches, 
                winnerBettors.length);
    }
    
    /**
     * @notice Get active players in a tournament
     * @param tournamentId Tournament ID
     */
    function getActivePlayers(uint256 tournamentId)
        external
        view
        tournamentExists(tournamentId)
        returns (
            address[] memory playerAddresses,
            bool[] memory isPlayerActive
        )
    {
        Tournament storage tournament = tournaments[tournamentId];
        
        // Count valid players
        uint8 playerCount = 0;
        for (uint8 i = 0; i < MAX_PLAYERS; i++) {
            address playerAddr = IStateStorage(stateStorage).getPlayerAtPosition(i);
            if (playerAddr != address(0)) {
                playerCount++;
            }
        }
        
        // Initialize arrays
        playerAddresses = new address[](playerCount);
        isPlayerActive = new bool[](playerCount);
        
        // Fill arrays
        uint8 index = 0;
        for (uint8 i = 0; i < MAX_PLAYERS; i++) {
            address playerAddr = IStateStorage(stateStorage).getPlayerAtPosition(i);
            if (playerAddr != address(0)) {
                playerAddresses[index] = playerAddr;
                isPlayerActive[index] = tournament.isActive[playerAddr];
                index++;
            }
        }
        
        return (playerAddresses, isPlayerActive);
    }
    
    /**
     * @notice Get all bettors for a tournament
     * @param tournamentId Tournament ID
     */
    function getAllBettors(uint256 tournamentId)
        external
        view
        tournamentExists(tournamentId)
        returns (address[] memory)
    {
        return tournaments[tournamentId].allBettors;
    }
    
    /**
     * @notice Get bettors for a specific player
     * @param tournamentId Tournament ID
     * @param player Player address
     */
    function getPlayerBettors(uint256 tournamentId, address player)
        external
        view
        tournamentExists(tournamentId)
        returns (address[] memory)
    {
        return tournaments[tournamentId].playerBettors[player];
    }
    
    /**
     * @notice Get user's bets for a tournament
     * @param tournamentId Tournament ID
     * @param bettor Address of the bettor
     */
    function getUserBets(uint256 tournamentId, address bettor)
        external
        view
        tournamentExists(tournamentId)
        returns (
            address[] memory players,
            uint256[] memory amounts
        )
    {
        // Count bets
        uint8 betCount = 0;
        for (uint8 i = 0; i < MAX_PLAYERS; i++) {
            address playerAddr = IStateStorage(stateStorage).getPlayerAtPosition(i);
            if (playerAddr != address(0) && bets[tournamentId][bettor][playerAddr].amount > 0) {
                betCount++;
            }
        }
        
        // Initialize arrays
        players = new address[](betCount);
        amounts = new uint256[](betCount);
        
        // Fill arrays
        uint8 index = 0;
        for (uint8 i = 0; i < MAX_PLAYERS; i++) {
            address playerAddr = IStateStorage(stateStorage).getPlayerAtPosition(i);
            if (playerAddr != address(0)) {
                PlayerBet storage bet = bets[tournamentId][bettor][playerAddr];
                if (bet.amount > 0) {
                    players[index] = playerAddr;
                    amounts[index] = bet.amount;
                    index++;
                }
            }
        }
    }
    
    /**
     * @notice Get potential payout for a bet
     * @param amount Bet amount
     */
    function getPotentialPayout(uint256 amount) external pure returns (uint256) {
        return amount + (amount * FIXED_ODDS / 100);
    }
    
    /**
     * @notice Get tournament status
     * @param tournamentId Tournament ID
     */
    function getTournamentStatus(uint256 tournamentId)
        external
        view
        tournamentExists(tournamentId)
        returns (
            BettingState state,
            uint256 totalPool,
            address winner,
            bool bettingIsOpen,
            uint256 bettorCount
        )
    {
        Tournament storage tournament = tournaments[tournamentId];
        return (
            tournament.state,
            tournament.totalPool,
            tournament.winner,
            tournament.state == BettingState.Open,
            tournament.allBettors.length
        );
    }
    
    /**
     * @notice Check if betting is open for a tournament
     * @param tournamentId Tournament ID
     */
    function isBettingOpen(uint256 tournamentId) external view returns (bool) {
        if (tournamentId > currentTournamentId) return false;
        return tournaments[tournamentId].state == BettingState.Open;
    }
    
    /**
     * @notice Receive function to allow contract to receive Ether
     */
    receive() external payable {}
}
