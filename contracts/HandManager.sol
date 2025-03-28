// SPDX-License-Identifier: MIT
pragma solidity ^0.8.18;

import './library.sol';
import './interfaces.sol';

/**
 * @title HandManager
 * @dev Handles all card dealing and hand management for poker games
 */
contract HandManager {
    using DeckManager for DeckManager.Deck;
    using PokerConstants for uint8;

    bool private dealing;

    DeckManager.Deck private deck;
    IStateStorage private stateStorage;
    IRouter private router;
    
    // Debug events
    event DebugFirstHandNotification(address tournamentLogic, uint256 timestamp);
    event DebugFirstHandNotificationSuccess(uint256 timestamp);
    event DebugFirstHandNotificationError(bytes errorReason, uint256 timestamp);

    event HandDealt(address indexed player, uint8 position);
    event CommunityCardsDealt(uint8[] cards, uint8 count);
    event HandRevealed(address indexed player, uint8[2] cards);

    modifier onlyValidState() {
        IStateStorage.TournamentState memory tournament = stateStorage
            .getTournamentState();
        require(
            tournament.tableState != IStateStorage.TableState.Complete,
            'Game is complete'
        );
        require(!tournament.isPaused, 'Game is paused');
        require(
            tournament.currentBlindLevel < 100,
            'Tournament blind cap reached'
        ); // Added safety check
        _;
    }

    modifier whileDealing() {
        require(!dealing, 'Cards being dealt');
        dealing = true;
        _;
        dealing = false;
    }

    constructor(address _stateStorage) {
        stateStorage = IStateStorage(_stateStorage);
        deck = DeckManager.initializeDeck();
        owner = msg.sender;
    }
    
    /**
     * @dev Set the router address after deployment
     * @param _router Address of the router contract
     */
    function setRouter(address _router) external onlyAdmin {
        require(_router != address(0), "Invalid router address");
        require(address(router) == address(0), "Router already set");
        router = IRouter(_router);
    }

    // Reference to TournamentLogic contract
    ITournamentLogic private tournamentLogic;
    
    /**
     * @dev Start a new hand, dealing cards to all players
     * @return dealerSeed The seed used for shuffling
     */
    function startNewHand()
        external
        onlyValidState
        returns (bytes32 dealerSeed)
    {
        // Check and update blind levels before starting the hand if needed
        _checkAndUpdateBlinds();
        
        // Get the latest blind values after potential update
        (
            uint256 smallBlind,
            uint256 bigBlind,
            uint8 buttonPos,
            uint8 activePlayerCount
        ) = _validateAndGetInitialState();

        // Handle blinds and positions
        (address sbPlayer, address bbPlayer) = _handleBlinds(
            buttonPos,
            smallBlind,
            bigBlind
        );

        // Deal cards and setup deck
        dealerSeed = _dealCards(
            buttonPos,
            activePlayerCount,
            sbPlayer,
            bbPlayer,
            smallBlind,
            bigBlind
        );

        // Set first player to act
        address firstToAct = _getNextActivePlayer(bbPlayer);
        
        // Calculate initial pot from blinds
        IStateStorage.Player memory sbPlayerState = stateStorage.getPlayer(sbPlayer);
        IStateStorage.Player memory bbPlayerState = stateStorage.getPlayer(bbPlayer);
        uint256 initialPot = sbPlayerState.currentBet + bbPlayerState.currentBet;
        
        stateStorage.updateGameBasics(
            uint8(IStateStorage.BettingRound.PreFlop),
            initialPot, // mainPot - include blinds
            bigBlind, // currentBet
            firstToAct // currentTurn
        );
        
        // Debug event for tracking first hand notification
        emit DebugFirstHandNotification(address(tournamentLogic), block.timestamp);

        // If TournamentLogic is set, notify it about the first hand start
        if (address(tournamentLogic) != address(0)) {
            // Trying to update the first hand start time in TournamentLogic
            try tournamentLogic.recordFirstHandStart() {
                // Successfully notified TournamentLogic
                emit DebugFirstHandNotificationSuccess(block.timestamp);
            } catch (bytes memory reason) {
                // Log error reason instead of silently ignoring
                emit DebugFirstHandNotificationError(reason, block.timestamp);
            }
        }

        return dealerSeed;
    }
    
    /**
     * @dev Check and update blind levels if needed before starting a hand
     */
    function _checkAndUpdateBlinds() private {
        // Only proceed if we have TournamentLogic set
        if (address(tournamentLogic) != address(0)) {
            try tournamentLogic.checkAndUpdateBlinds() {
                // Successfully checked and updated blinds if needed
                // No need to do anything else - the tournament state is updated
            } catch {
                // Silently ignore errors - shouldn't prevent hand from starting
            }
        }
    }
    
    // Admin role
    address private immutable owner;
    mapping(address => bool) private admins;
    
    /**
     * @dev Check if caller is admin or owner
     */
    modifier onlyAdmin() {
        require(msg.sender == owner || admins[msg.sender], "Not authorized");
        _;
    }
    
    /**
     * @dev Add an address to admins
     * @param admin Address to add as admin
     */
    function addAdmin(address admin) external {
        require(msg.sender == owner, "Only owner can add admins");
        require(admin != address(0), "Invalid address");
        admins[admin] = true;
    }
    
    /**
     * @dev Remove an address from admins
     * @param admin Address to remove from admins
     */
    function removeAdmin(address admin) external {
        require(msg.sender == owner, "Only owner can remove admins");
        require(admin != owner, "Cannot remove owner");
        admins[admin] = false;
    }
    
    /**
     * @dev Set the TournamentLogic contract reference
     * @param _tournamentLogic Address of the TournamentLogic contract
     */
    function setTournamentLogic(address _tournamentLogic) external onlyAdmin {
        require(_tournamentLogic != address(0), "Invalid address");
        tournamentLogic = ITournamentLogic(_tournamentLogic);
    }

    function _validateAndGetInitialState()
        private
        view
        returns (
            uint256 smallBlind,
            uint256 bigBlind,
            uint8 buttonPos,
            uint8 activePlayerCount
        )
    {
        uint256 _smallBlind;
        uint256 _bigBlind;
        uint8 _tableState;
        uint8 _buttonPos;
        uint8 _activePlayerCount;
        bool _isPaused;

        // Get tournament state values
        (
            _smallBlind,
            _bigBlind, // blindTimer
            ,
            ,
            // lastBlindUpdate
            _tableState,
            _buttonPos, // dealerPos
            _activePlayerCount, // startTime
            ,
            _isPaused,
            // currentBlindLevel

        ) = stateStorage.getTournamentStateValues();

        require(
            _tableState == uint8(IStateStorage.TableState.Active),
            'Tournament not active'
        );
        require(!_isPaused, 'Tournament is paused');
        require(_activePlayerCount >= 2, 'Not enough players');

        return (_smallBlind, _bigBlind, _buttonPos, _activePlayerCount);
    }

    function _handleBlinds(
        uint8 buttonPos,
        uint256 smallBlind,
        uint256 bigBlind
    ) private returns (address sbPlayer, address bbPlayer) {
        // Get active player count to check for heads-up play
        uint8 activeCount = 0;
        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address playerAddr = stateStorage.getPlayerAtPosition(i);
            if (playerAddr != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(
                    playerAddr
                );
                if (player.status == IStateStorage.PlayerStatus.Active) {
                    activeCount++;
                }
            }
        }

        // Heads-up play (2 players) - SB is on button, BB is off button
        if (activeCount == 2) {
            uint8 sbPos = buttonPos; // SB is on the button in heads-up
            uint8 bbPos = 0;

            // Find BB position (first active player that's not on the button)
            for (uint8 i = 1; i <= PokerConstants.MAX_PLAYERS; i++) {
                uint8 pos = (buttonPos + i) % PokerConstants.MAX_PLAYERS;
                address playerAddr = stateStorage.getPlayerAtPosition(pos);
                if (playerAddr != address(0)) {
                    IStateStorage.Player memory player = stateStorage.getPlayer(
                        playerAddr
                    );
                    if (player.status == IStateStorage.PlayerStatus.Active) {
                        bbPos = pos;
                        break;
                    }
                }
            }

            sbPlayer = stateStorage.getPlayerAtPosition(sbPos);
            bbPlayer = stateStorage.getPlayerAtPosition(bbPos);
        }
        // Normal play (3+ players) - SB is 1 position after button, BB is 2 positions after
        else {
            // Use the proper blind positions determination from TournamentLogic
            (uint8 sbPos, uint8 bbPos) = _getValidBlindsPositions(buttonPos);

            sbPlayer = stateStorage.getPlayerAtPosition(sbPos);
            bbPlayer = stateStorage.getPlayerAtPosition(bbPos);
        }

        IStateStorage.Player memory sbPlayerState = stateStorage.getPlayer(
            sbPlayer
        );
        IStateStorage.Player memory bbPlayerState = stateStorage.getPlayer(
            bbPlayer
        );

        // For SB, if they don't have enough for the full small blind, they go all-in
        if (sbPlayerState.stack < smallBlind) {
            // Player goes all-in with what they have
            sbPlayerState.currentBet = sbPlayerState.stack;
            sbPlayerState.totalContribution += sbPlayerState.stack;
            // Mark player as all-in
            sbPlayerState.status = IStateStorage.PlayerStatus.AllIn;
            // Set stack to 0
            sbPlayerState.stack = 0;
        } else {
            // Normal case - player has enough for the small blind
            sbPlayerState.stack -= smallBlind;
            sbPlayerState.currentBet = smallBlind;
            sbPlayerState.totalContribution += smallBlind;
        }
        
        // For BB, if they don't have enough for the full big blind, they go all-in
        if (bbPlayerState.stack < bigBlind) {
            // Player goes all-in with what they have
            bbPlayerState.currentBet = bbPlayerState.stack;
            bbPlayerState.totalContribution += bbPlayerState.stack;
            // Mark player as all-in
            bbPlayerState.status = IStateStorage.PlayerStatus.AllIn;
            // Set stack to 0
            bbPlayerState.stack = 0;
        } else {
            // Normal case - player has enough for the big blind
            bbPlayerState.stack -= bigBlind;
            bbPlayerState.currentBet = bigBlind;
            bbPlayerState.totalContribution += bigBlind;
        }

        stateStorage.updatePlayerState(sbPlayer, sbPlayerState);
        stateStorage.updatePlayerState(bbPlayer, bbPlayerState);

        return (sbPlayer, bbPlayer);
    }

    // Get valid blind positions (adapted from TournamentLogic.getValidBlindsPositions)
    // TODO: Refactor this code!
    // getValidBlindsPositions also exists in Tournament Logic
    function _getValidBlindsPositions(
        uint8 buttonPos
    ) private view returns (uint8 sb, uint8 bb) {
        // Find small blind position
        uint8 sbPos = (buttonPos + 1) % PokerConstants.MAX_PLAYERS;
        bool foundSB = false;

        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            uint8 pos = (sbPos + i) % PokerConstants.MAX_PLAYERS;
            address playerAddr = stateStorage.getPlayerAtPosition(pos);

            if (playerAddr != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(
                    playerAddr
                );
                if (player.status == IStateStorage.PlayerStatus.Active) {
                    sbPos = pos;
                    foundSB = true;
                    break;
                }
            }
        }

        require(foundSB, 'No valid SB position');

        // Find big blind position
        uint8 bbPos = (sbPos + 1) % PokerConstants.MAX_PLAYERS;
        bool foundBB = false;

        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            uint8 pos = (bbPos + i) % PokerConstants.MAX_PLAYERS;
            address playerAddr = stateStorage.getPlayerAtPosition(pos);

            if (playerAddr != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(
                    playerAddr
                );
                if (player.status == IStateStorage.PlayerStatus.Active) {
                    bbPos = pos;
                    foundBB = true;
                    break;
                }
            }
        }

        require(foundBB, 'No valid BB position');

        return (sbPos, bbPos);
    }

    function _dealCards(
        uint8 buttonPos,
        uint8 activePlayerCount,
        address sbPlayer,
        address bbPlayer,
        uint256 smallBlind,
        uint256 bigBlind
    ) private returns (bytes32) {
        // Initialize game state for new hand
        uint8[5] memory emptyCards = [0, 0, 0, 0, 0];
        stateStorage.updateGameCards(emptyCards);
        stateStorage.updateGameBasics(
            uint8(IStateStorage.BettingRound.PreFlop),
            0, // mainPot
            bigBlind, // currentBet
            address(0) // currentTurn will be set after dealing
        );
        stateStorage.updateGameTimers(30 seconds, block.timestamp);

        // Reset deck and get seed
        deck = DeckManager.initializeDeck();
        deck.shuffle();
        bytes32 dealerSeed = deck.lastSeed;

        // Deal cards to players
        uint8 currentPosition = buttonPos;
        uint8 dealtCount = 0;

        while (dealtCount < activePlayerCount) {
            currentPosition =
                (currentPosition + 1) %
                PokerConstants.MAX_PLAYERS;
            address playerAddr = stateStorage.getPlayerAtPosition(
                currentPosition
            );

            if (playerAddr != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(
                    playerAddr
                );
                // Deal cards to active players AND all-in players
                // This ensures players who went all-in from blinds still get cards
                if (
                    (player.status == IStateStorage.PlayerStatus.Active && player.stack > 0) ||
                    player.status == IStateStorage.PlayerStatus.AllIn
                ) {
                    // For non-blind players, current bet is 0
                    // For blind players, these values were already set in _handleBlinds
                    if (playerAddr != sbPlayer && playerAddr != bbPlayer) {
                        player.currentBet = 0;
                    }

                    player.holeCards = deck.dealHoleCards(currentPosition);
                    stateStorage.updatePlayerState(playerAddr, player);
                    emit HandDealt(playerAddr, currentPosition);
                    dealtCount++;
                }
            }
        }

        return dealerSeed;
    }

    function _getNextActivePlayer(
        address currentPlayer
    ) private view returns (address) {
        IStateStorage.Player memory player = stateStorage.getPlayer(
            currentPlayer
        );
        uint8 currentPosition = player.position;

        for (uint8 i = 1; i <= PokerConstants.MAX_PLAYERS; i++) {
            uint8 nextPosition = (currentPosition + i) %
                PokerConstants.MAX_PLAYERS;
            address playerAtPosition = stateStorage.getPlayerAtPosition(
                nextPosition
            );

            if (playerAtPosition != address(0)) {
                IStateStorage.Player memory nextPlayer = stateStorage.getPlayer(
                    playerAtPosition
                );
                if (
                    nextPlayer.status == IStateStorage.PlayerStatus.Active &&
                    nextPlayer.stack > 0
                ) {
                    return playerAtPosition;
                }
            }
        }

        revert('No active players found');
    }

    /**
     * @dev Deal the flop (3 community cards)
     * @return cards The three flop cards
     */
    function dealFlop() external whileDealing returns (uint8[] memory) {
        IStateStorage.GameState memory gameState = stateStorage.getGameState();
        require(
            gameState.currentRound == IStateStorage.BettingRound.PreFlop,
            'Not time for flop'
        );

        uint8[] memory flopCards = deck.dealCommunityCards(3);
        emit CommunityCardsDealt(flopCards, 3);
        
        // Note: Round advancement is now handled by Router.dealFlopAndAdvance()
        return flopCards;
    }

    /**
     * @dev Deal the turn (1 community card)
     * @return card The turn card
     */
    function dealTurn() external whileDealing returns (uint8[] memory) {
        IStateStorage.GameState memory gameState = stateStorage.getGameState();
        require(
            gameState.currentRound == IStateStorage.BettingRound.Flop,
            'Not time for turn'
        );

        uint8[] memory turnCard = deck.dealCommunityCards(1);
        emit CommunityCardsDealt(turnCard, 1);
        
        // Note: Round advancement is now handled by Router.dealTurnAndAdvance()
        return turnCard;
    }

    /**
     * @dev Deal the river (1 community card)
     * @return card The river card
     */
    function dealRiver() external whileDealing returns (uint8[] memory) {
        IStateStorage.GameState memory gameState = stateStorage.getGameState();
        require(
            gameState.currentRound == IStateStorage.BettingRound.Turn,
            'Not time for river'
        );

        uint8[] memory riverCard = deck.dealCommunityCards(1);
        emit CommunityCardsDealt(riverCard, 1);
        
        // Note: Round advancement is now handled by Router.dealRiverAndAdvance()
        return riverCard;
    }

    /**
     * @dev Reveal a player's hand (used for both folded hands and showdown)
     * @param player Address of the player whose hand to reveal
     */
    function revealHand(address player) external onlyValidState {
        IStateStorage.Player memory playerState = stateStorage.getPlayer(
            player
        );
        IStateStorage.GameState memory gameState = stateStorage.getGameState();

        require(
            playerState.status == IStateStorage.PlayerStatus.Folded ||
                gameState.currentRound == IStateStorage.BettingRound.River,
            'Can only reveal folded hands or during showdown'
        );

        emit HandRevealed(player, playerState.holeCards);
    }

    //Debug
    // Test the first step
    function testValidateState()
        external
        view
        returns (
            uint256 smallBlind,
            uint256 bigBlind,
            uint8 buttonPos,
            uint8 activePlayerCount
        )
    {
        return _validateAndGetInitialState();
    }

    // Test blind handling
    function testHandleBlinds(
        uint8 buttonPos,
        uint256 smallBlind,
        uint256 bigBlind
    ) external returns (address sbPlayer, address bbPlayer) {
        return _handleBlinds(buttonPos, smallBlind, bigBlind);
    }

    // Test card dealing setup
    function testDealSetup() external {
        uint8[5] memory emptyCards = [0, 0, 0, 0, 0];
        stateStorage.updateGameCards(emptyCards);
        stateStorage.updateGameBasics(
            uint8(IStateStorage.BettingRound.PreFlop),
            0, // mainPot
            50, // currentBet (using default big blind)
            address(0) // currentTurn
        );
        stateStorage.updateGameTimers(30 seconds, block.timestamp);
    }

    function testDeckSetup() external returns (bytes32) {
        // Reset deck and get seed
        deck = DeckManager.initializeDeck();
        deck.shuffle();
        return deck.lastSeed;
    }
}
