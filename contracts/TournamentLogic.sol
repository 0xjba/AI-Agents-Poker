// SPDX-License-Identifier: MIT
pragma solidity ^0.8.18;

import './library.sol';
import './interfaces.sol';

/**
 * @title TournamentLogic
 * @dev Handles poker tournament logic including blind progression and player elimination
 */
contract TournamentLogic is ITournamentLogic {
    using PokerConstants for uint8;

    IStateStorage private immutable stateStorage;
    address private immutable owner;

    uint256 private constant INITIAL_SMALL_BLIND = 25;
    uint256 private constant INITIAL_BIG_BLIND = 50;
    uint256 private constant BLIND_LEVEL_DURATION = 5 minutes;
    uint256 private constant SMALL_BLIND_INCREMENT = 25;
    uint256 private constant BIG_BLIND_INCREMENT = 50;
    uint256 private constant LEVELS_BEFORE_DOUBLE = 3;
    uint256 private constant MAX_SMALL_BLIND = 10000; // Safety cap
    uint256 private constant INITIAL_STACK = 200;

    modifier onlyValidPlayers(address[] memory players) {
        require(
            players.length >= 2 && players.length <= PokerConstants.MAX_PLAYERS,
            'Invalid player count'
        );
        for (uint i = 0; i < players.length; i++) {
            require(players[i] != address(0), 'Invalid player address');
            for (uint j = i + 1; j < players.length; j++) {
                require(players[i] != players[j], 'Duplicate player');
            }
        }
        _;
    }

    struct BlindLevel {
        uint256 smallBlind;
        uint256 bigBlind;
        uint256 startTime;
    }

    BlindLevel[] private blindLevels;

    // Existing events
    event BlindLevelProgression(
        uint256 indexed level,
        uint256 smallBlind,
        uint256 bigBlind,
        bool isDoubling
    );

    // New events for better tournament state tracking
    event TournamentStateChanged(
        uint8 tableState,
        uint8 activePlayerCount,
        bool isPaused
    );

    event TournamentBlindsUpdated(
        uint256 smallBlind,
        uint256 bigBlind,
        uint256 currentLevel,
        uint256 timestamp
    );

    event ButtonRotated(uint8 newButtonPosition);
    
    // First hand tracking for blind timing
    // event defined in ITournamentLogic interface

    constructor(address _stateStorage) {
        stateStorage = IStateStorage(_stateStorage);
        owner = msg.sender;
    }

    // Poker betting contract address
    address private pokerBettingContract;
    
    /**
     * @notice Set the poker betting contract address
     * @param _pokerBettingContract Address of the betting contract
     */
    function setPokerBettingContract(address _pokerBettingContract) external {
        require(msg.sender == owner, 'Only owner');
        require(_pokerBettingContract != address(0), 'Invalid address');
        pokerBettingContract = _pokerBettingContract;
    }
    
    /**
     * @notice Start a new tournament with given players
     * @param players Array of player addresses
     */
    function startTournament(
        address[] memory players
    ) external onlyValidPlayers(players) {
        // Check if tournament is already active
        IStateStorage.TournamentState memory tournament = stateStorage
            .getTournamentState();
        require(
            tournament.tableState != IStateStorage.TableState.Active,
            'Tournament already active'
        );

        // Update blinds first
        stateStorage.updateTournamentBlinds(
            INITIAL_SMALL_BLIND,
            INITIAL_BIG_BLIND
        );

        // Update status
        stateStorage.updateTournamentStatus(
            IStateStorage.TableState.Active,
            uint8(players.length),
            false // not paused
        );

        // Update positions
        stateStorage.updateTournamentPositions(0);

        // Initialize player states
        for (uint8 i = 0; i < players.length; i++) {
            IStateStorage.Player memory player;
            player.stack = INITIAL_STACK;
            player.status = IStateStorage.PlayerStatus.Active;
            player.position = i;
            player.playerIndex = i;
            stateStorage.updatePlayerState(players[i], player);
        }

        // Emit standard tournament start event
        emit TournamentStarted(block.timestamp);

        // Also emit detailed state change event
        emit TournamentStateChanged(
            uint8(IStateStorage.TableState.Active),
            uint8(players.length),
            false
        );

        // Emit initial blinds event
        emit TournamentBlindsUpdated(
            INITIAL_SMALL_BLIND,
            INITIAL_BIG_BLIND,
            0, // Initial level
            block.timestamp
        );
        
        // Notify the betting contract that a tournament has started
        if (pokerBettingContract != address(0)) {
            (bool success, ) = pokerBettingContract.call(
                abi.encodeWithSignature('handleTournamentStart(address[])', players)
            );
            // We don't want to revert the tournament start if the notification fails
        }
    }

    /**
     * @notice Update blind levels based on time elapsed
     */
    function updateBlinds() external {
        IStateStorage.TournamentState memory tournament = stateStorage
            .getTournamentState();
        require(
            tournament.tableState == IStateStorage.TableState.Active,
            'Tournament not active'
        );
        require(!tournament.isPaused, 'Tournament paused');

        uint256 currentLevel = getCurrentBlindLevel();
        uint256 expectedLevel = getExpectedBlindLevel();
        
        // Only update if we need to increase level
        if (expectedLevel > currentLevel) {
            // Calculate how many levels to increase at once (for cases where multiple levels were missed)
            uint256 levelIncrease = expectedLevel - currentLevel;
            
            // Get the new blinds based on the expected level
            (uint256 newSmallBlind, uint256 newBigBlind) = calculateNextBlinds(
                expectedLevel
            );

            // Essential tournament rule: cap maximum blinds
            if (newSmallBlind > MAX_SMALL_BLIND) {
                newSmallBlind = MAX_SMALL_BLIND;
                newBigBlind = MAX_SMALL_BLIND * 2;
            }

            // Update tournament state
            tournament.smallBlind = newSmallBlind;
            tournament.bigBlind = newBigBlind;
            tournament.lastBlindUpdate = block.timestamp;
            tournament.currentBlindLevel = expectedLevel;

            // Add new blind level to history
            IStateStorage.BlindLevel memory newLevel = IStateStorage
                .BlindLevel({
                    smallBlind: newSmallBlind,
                    bigBlind: newBigBlind,
                    startTime: block.timestamp
                });
            stateStorage.addBlindLevel(newLevel);

            // Emit event for blind increase with level information
            emit BlindsIncreased(newSmallBlind, newBigBlind);
            emit TournamentBlindsUpdated(
                newSmallBlind,
                newBigBlind,
                expectedLevel,
                block.timestamp
            );

            // Essential tournament rule: end tournament if blinds too high relative to stacks
            uint256 avgStack = _calculateAverageStack();
            if (tournament.smallBlind > avgStack / 4) {
                // Use current small blind
                _completeTournament();
                return;
            }

            stateStorage.updateTournamentState(tournament);
        }
    }

    // Essential helper function for blind-based tournament completion
    function _calculateAverageStack() private view returns (uint256) {
        IStateStorage.TournamentState memory tournament = stateStorage
            .getTournamentState();
        if (tournament.activePlayerCount == 0) return 0;

        uint256 totalChips = 0;
        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address playerAddr = stateStorage.getPlayerAtPosition(i);
            if (playerAddr != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(
                    playerAddr
                );
                if (player.status == IStateStorage.PlayerStatus.Active) {
                    totalChips += player.stack;
                }
            }
        }

        return totalChips / tournament.activePlayerCount;
    }

    /**
     * @notice Calculate next blinds based on target level
     * @param targetLevel The target blind level
     * @return smallBlind The new small blind
     * @return bigBlind The new big blind
     */
    function calculateNextBlinds(
        uint256 targetLevel
    ) private view returns (uint256, uint256) {
        // Get current tournament state
        IStateStorage.TournamentState memory tournament = stateStorage
            .getTournamentState();
        uint256 smallBlind = tournament.smallBlind;
        uint256 bigBlind = tournament.bigBlind;
        uint256 currentLevel = tournament.currentBlindLevel;
        
        // Optimization for large level jumps
        if (targetLevel > currentLevel + 10) {
            // Calculate number of levels to process
            uint256 levelDifference = targetLevel - currentLevel;
            
            // Calculate number of doubling events that will occur
            uint256 currentLevelCycle = currentLevel / LEVELS_BEFORE_DOUBLE;
            uint256 targetLevelCycle = targetLevel / LEVELS_BEFORE_DOUBLE;
            uint256 doublingEvents = targetLevelCycle - currentLevelCycle;
            
            // Calculate number of standard increment levels
            uint256 standardIncrementLevels = levelDifference;
            
            // If the current level is a multiple of LEVELS_BEFORE_DOUBLE, we need to adjust
            if (currentLevel % LEVELS_BEFORE_DOUBLE == 0 && currentLevel > 0) {
                doublingEvents--; // Already counted in current blinds
            }
            
            // Apply doubling events first (these have the largest impact)
            for (uint256 i = 0; i < doublingEvents; i++) {
                smallBlind = smallBlind * 2;
                bigBlind = bigBlind * 2;
            }
            
            // Calculate remaining standard increments after doubling
            uint256 remainingStandardLevels = targetLevel;
            // Subtract the levels that were doubled
            if (doublingEvents > 0) {
                remainingStandardLevels = targetLevel % LEVELS_BEFORE_DOUBLE;
                // If we end exactly on a doubling level, no standard increments needed
                if (remainingStandardLevels == 0 && targetLevel > 0) {
                    remainingStandardLevels = LEVELS_BEFORE_DOUBLE;
                }
            }
            
            // Apply standard increments for remaining levels
            smallBlind += (remainingStandardLevels * SMALL_BLIND_INCREMENT);
            bigBlind += (remainingStandardLevels * BIG_BLIND_INCREMENT);
        } else {
            // For smaller jumps, use the original iterative approach
            // (more readable and less prone to edge case errors)
            for (uint256 i = currentLevel + 1; i <= targetLevel; i++) {
                // Check if this level requires doubling
                if (i % LEVELS_BEFORE_DOUBLE == 0) {
                    // Double the blinds
                    smallBlind = smallBlind * 2;
                    bigBlind = bigBlind * 2;
                } else {
                    // Standard increment
                    smallBlind += SMALL_BLIND_INCREMENT;
                    bigBlind += BIG_BLIND_INCREMENT;
                }
            }
        }

        return (smallBlind, bigBlind);
    }

    function getCurrentBlindLevel() public view returns (uint256) {
        IStateStorage.TournamentState memory tournament = stateStorage
            .getTournamentState();
        return tournament.currentBlindLevel;
    }

    // Track the start time of the first hand for blind timing
    // Changed to public for direct verification during debugging
    uint256 public firstHandStartTime;
    
    // Debug event to track firstHandStartTime value
    event DebugFirstHandStartTime(uint256 currentValue, uint256 timestamp);
    
    /**
     * @notice Record the first hand start time for blind timing
     * @dev Called by the HandManager when the first hand starts
     */
    function recordFirstHandStart() external {
        // Emit debug event before setting
        emit DebugFirstHandStartTime(firstHandStartTime, block.timestamp);
        
        // Only allow setting this once
        if (firstHandStartTime == 0) {
            firstHandStartTime = block.timestamp;
            emit FirstHandStarted(block.timestamp);
        }
    }
    
    /**
     * @notice Debug function to check firstHandStartTime value
     * @dev This is for debugging only and should be removed in production
     */
    function debugGetFirstHandStartTime() external view returns (uint256) {
        return firstHandStartTime;
    }
    
    // Debug event to track blind level calculation details
    event DebugBlindLevelCalculation(
        uint256 firstHandStartTime,
        uint256 tournamentStartTime,
        uint256 blindReferenceTime,
        uint256 currentTimestamp,
        uint256 timeElapsed,
        uint256 blindLevelDuration,
        uint256 calculatedLevel,
        uint256 currentLevel
    );
    
    function getExpectedBlindLevel() public view returns (uint256) {
        IStateStorage.TournamentState memory tournament = stateStorage
            .getTournamentState();
        if (tournament.tableState != IStateStorage.TableState.Active) {
            return 0;
        }
        
        // Reference time for blind level calculations:
        // 1. If first hand start time is set, use that as reference
        // 2. If no hands have started yet, use tournament start time
        uint256 blindReferenceTime;
        
        if (firstHandStartTime > 0) {
            // We have a stored first hand start time, use that
            blindReferenceTime = firstHandStartTime;
        } else {
            // No hands have started yet, use tournament start time
            blindReferenceTime = tournament.startTime;
        }
        
        // Calculate expected level based on time elapsed since the reference time
        uint256 timeElapsed = block.timestamp - blindReferenceTime;
        uint256 level = timeElapsed / BLIND_LEVEL_DURATION;
        
        // Log details for debugging (these are view-only so don't generate events)
        // Used for tournament analysis and debugging timing issues
        if (level > tournament.currentBlindLevel + 2) {
            // Large blind level jump detected (more than 2 levels)
            // This is normal when the first hand starts after a long betting window
        }
        
        return level;
    }
    
    /**
     * @notice Debug function to get blind level calculation details
     * @dev This is for debugging only and should be removed in production
     */
    function debugGetBlindLevelCalculation() external {
        IStateStorage.TournamentState memory tournament = stateStorage
            .getTournamentState();
            
        uint256 blindReferenceTime;
        if (firstHandStartTime > 0) {
            blindReferenceTime = firstHandStartTime;
        } else {
            blindReferenceTime = tournament.startTime;
        }
        
        uint256 timeElapsed = block.timestamp - blindReferenceTime;
        uint256 calculatedLevel = timeElapsed / BLIND_LEVEL_DURATION;
        
        emit DebugBlindLevelCalculation(
            firstHandStartTime,
            tournament.startTime,
            blindReferenceTime,
            block.timestamp,
            timeElapsed,
            BLIND_LEVEL_DURATION,
            calculatedLevel,
            tournament.currentBlindLevel
        );
    }

    function shouldForceTournamentEnd(
        uint256 newSmallBlind
    ) private view returns (bool) {
        uint256 totalChips = 0;
        uint256 activePlayers = 0;

        // Calculate average stack
        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address playerAddress = stateStorage.getPlayerAtPosition(i);
            if (playerAddress != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(
                    playerAddress
                );
                if (player.status == IStateStorage.PlayerStatus.Active) {
                    totalChips += player.stack;
                    activePlayers++;
                }
            }
        }

        if (activePlayers == 0) return false;

        uint256 averageStack = totalChips / activePlayers;
        // Force end if small blind is more than 1/4 of average stack
        return newSmallBlind > (averageStack / 4);
    }

    function getBlindLevelHistory()
        external
        view
        returns (BlindLevel[] memory)
    {
        return blindLevels;
    }

    /**
     * @notice Process player elimination
     * @param player Address of eliminated player
     */
    function processElimination(address player) external {
        IStateStorage.Player memory playerState = stateStorage.getPlayer(
            player
        );

        // Get tournament state values
        (
            uint256 smallBlind,
            uint256 bigBlind, // blindTimer
            ,
            ,
            // lastBlindUpdate
            uint8 tableState, // buttonPosition
            ,
            uint8 activeCount, // startTime
            ,
            bool isPaused,
            uint256 currentLevel
        ) = stateStorage.getTournamentStateValues();

        require(
            tableState == uint8(IStateStorage.TableState.Active),
            'Tournament not active'
        );
        require(!isPaused, 'Tournament paused');
        require(
            playerState.status == IStateStorage.PlayerStatus.Active,
            'Player not active'
        );
        require(playerState.stack == 0, 'Player still has chips');

        // Update player status to eliminated
        playerState.status = IStateStorage.PlayerStatus.Eliminated;

        // Clear player's position in the mapping
        uint8 position = playerState.position;
        stateStorage.updatePlayerState(player, playerState);

        // Confirm player is no longer at the position
        address playerAtPosition = stateStorage.getPlayerAtPosition(position);
        if (playerAtPosition == player) {
            // If player is still mapped to position, update it to address(0)
            playerState.position = 0; // Reset position
            stateStorage.updatePlayerState(player, playerState);
        }

        // Verify active count before decrementing
        uint8 verifiedActiveCount = _getActivePlayerCount();

        // Update tournament state using the specific function
        stateStorage.updateTournamentStatus(
            IStateStorage.TableState.Active,
            verifiedActiveCount,
            isPaused
        );

        // Emit player eliminated event
        emit PlayerEliminated(player);

        // Emit tournament state changed event
        emit TournamentStateChanged(
            uint8(IStateStorage.TableState.Active),
            verifiedActiveCount,
            isPaused
        );

        // Also emit current blinds info for convenience
        emit TournamentBlindsUpdated(
            smallBlind,
            bigBlind,
            currentLevel,
            block.timestamp
        );

        // Check if tournament is complete
        if (verifiedActiveCount == 1) {
            _completeTournament();
        }
    }

    /**
     * @notice Check current tournament status
     * @return isComplete Whether tournament is complete
     * @return winner Address of winner if complete
     */
    function checkTournamentStatus()
        external
        view
        returns (bool isComplete, address winner)
    {
        IStateStorage.TournamentState memory tournament = stateStorage
            .getTournamentState();

        if (
            tournament.tableState == IStateStorage.TableState.Complete &&
            tournament.activePlayerCount == 1
        ) {
            // Find the last active player
            for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
                address playerAddress = stateStorage.getPlayerAtPosition(i);
                if (playerAddress != address(0)) {
                    IStateStorage.Player memory player = stateStorage.getPlayer(
                        playerAddress
                    );
                    if (player.status == IStateStorage.PlayerStatus.Active) {
                        return (true, playerAddress);
                    }
                }
            }
        }

        return (false, address(0));
    }

    /**
     * @dev Complete the tournament and declare winner
     */
    function _completeTournament() private {
        address winner = _findTournamentWinner();
        require(winner != address(0), 'No winner found');

        // Reset game state
        _resetGameState();
        
        // Reset player states
        _resetPlayerStates(winner);

        // Update tournament state
        IStateStorage.TournamentState memory tournament = stateStorage.getTournamentState();
        tournament.tableState = IStateStorage.TableState.Complete;
        stateStorage.updateTournamentState(tournament);

        // Emit tournament completed event
        emit TournamentCompleted(winner);

        // Also emit state change event for consistent tracking
        emit TournamentStateChanged(
            uint8(IStateStorage.TableState.Complete),
            1, // Only winner remains
            tournament.isPaused
        );
        
        // Notify the betting contract that the tournament has completed
        if (pokerBettingContract != address(0)) {
            // Get current tournament ID (assuming it's 1 since we don't track it explicitly)
            uint256 tournamentId = 1;
            (bool success, ) = pokerBettingContract.call(
                abi.encodeWithSignature('handleTournamentComplete(uint256,address)', tournamentId, winner)
            );
            // We don't need to handle failure here as it won't affect tournament completion
            // But we do check the return value to satisfy the compiler warning
            if (!success) {
                // Silent failure - tournament completion shouldn't be affected by betting contract
            }
        }
    }
    
    /**
     * @dev Find the tournament winner
     */
    function _findTournamentWinner() private view returns (address) {
        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address playerAddress = stateStorage.getPlayerAtPosition(i);
            if (playerAddress != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(playerAddress);
                if (player.status == IStateStorage.PlayerStatus.Active) {
                    return playerAddress;
                }
            }
        }
        return address(0);
    }
    
    /**
     * @dev Reset game state to initial values
     */
    function _resetGameState() private {
        IStateStorage.GameState memory gameState = stateStorage.getGameState();

        // Clear community cards and reset values
        gameState.communityCards = [0, 0, 0, 0, 0];
        gameState.currentRound = IStateStorage.BettingRound.PreFlop;
        gameState.mainPot = 0;
        gameState.currentBet = 0;
        gameState.lastRaise = 0;
        gameState.minRaise = 0;
        gameState.lastAggressor = 0;
        gameState.currentTurn = address(0);
        gameState.lastActionAmount = 0;
        gameState.handStartTime = 0;
        gameState.actionTimer = 0;

        // Update state
        stateStorage.updateGameState(gameState);
        stateStorage.resetPlayerActions();

        // Reset side pots
        uint256 totalSidePots = stateStorage.sidePotCount();
        for (uint256 i = 0; i < totalSidePots; i++) {
            stateStorage.setSidePotResolved(i);
        }
    }
    
    /**
     * @dev Reset player states
     */
    function _resetPlayerStates(address winner) private {
        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address playerAddr = stateStorage.getPlayerAtPosition(i);
            if (playerAddr != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(playerAddr);

                // Reset player data
                player.holeCards = [0, 0];
                player.currentBet = 0;
                player.totalContribution = 0;

                // Set non-winners to Eliminated
                if (playerAddr != winner && player.status != IStateStorage.PlayerStatus.Eliminated) {
                    player.status = IStateStorage.PlayerStatus.Eliminated;
                }

                stateStorage.updatePlayerState(playerAddr, player);
            }
        }
    }

    /**
     * @notice Get current tournament progress
     * @return elapsedTime Time elapsed since start
     * @return blindLevel Current blind level
     * @return remainingPlayers Number of active players
     */
    function getTournamentProgress()
        external
        view
        returns (
            uint256 elapsedTime,
            uint256 blindLevel,
            uint8 remainingPlayers
        )
    {
        IStateStorage.TournamentState memory tournament = stateStorage
            .getTournamentState();

        if (tournament.tableState == IStateStorage.TableState.Active) {
            elapsedTime = block.timestamp - tournament.startTime;
            blindLevel = (elapsedTime / tournament.blindTimer) + 1;
            remainingPlayers = tournament.activePlayerCount;
        } else {
            elapsedTime = 0;
            blindLevel = 0;
            remainingPlayers = 0;
        }
        return (elapsedTime, blindLevel, remainingPlayers);
    }

    function rotateButton() internal returns (uint8) {
        IStateStorage.TournamentState memory tournament = stateStorage.getTournamentState();
        uint8 newButton = tournament.buttonPosition;
        
        // Find next valid position
        for (uint8 i = 1; i <= PokerConstants.MAX_PLAYERS; i++) {
            uint8 candidatePos = (newButton + i) % PokerConstants.MAX_PLAYERS;
            address playerAddr = stateStorage.getPlayerAtPosition(candidatePos);
            
            if (playerAddr != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(playerAddr);
                if (player.status == IStateStorage.PlayerStatus.Active) {
                    tournament.buttonPosition = candidatePos;
                    stateStorage.updateTournamentState(tournament);
                    emit ButtonRotated(candidatePos);
                    return candidatePos;
                }
            }
        }
        
        revert('No active players for button');
    }

    function getValidBlindsPositions(
        uint8 buttonPos
    ) internal view returns (uint8 sb, uint8 bb) {
        // Find small blind position (SB)
        uint8 checkPos = (buttonPos + 1) % PokerConstants.MAX_PLAYERS;
        uint8 sbPos = 0;
        bool foundSB = false;
        
        // Find active players for SB and BB positions
        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS * 2; i++) {
            address playerAddr = stateStorage.getPlayerAtPosition(checkPos);
            if (playerAddr != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(playerAddr);
                if (player.status == IStateStorage.PlayerStatus.Active) {
                    if (!foundSB) {
                        // Found SB position
                        sbPos = checkPos;
                        foundSB = true;
                    } else {
                        // Found BB position
                        return (sbPos, checkPos);
                    }
                }
            }
            checkPos = (checkPos + 1) % PokerConstants.MAX_PLAYERS;
        }
        
        revert(foundSB ? 'No valid BB position' : 'No valid SB position');
    }

    // Helper function to get accurate active player count
    function _getActivePlayerCount() private view returns (uint8) {
        uint8 count = 0;

        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address playerAddress = stateStorage.getPlayerAtPosition(i);
            if (playerAddress != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(
                    playerAddress
                );
                if (player.status == IStateStorage.PlayerStatus.Active) {
                    count++;
                }
            }
        }

        return count;
    }

    function updateButtonPosition() public {
        IStateStorage.TournamentState memory tournament = stateStorage.getTournamentState();
        uint8 currentPos = tournament.buttonPosition;
        
        // Find next active player
        for (uint8 i = 1; i <= PokerConstants.MAX_PLAYERS; i++) {
            uint8 candidatePos = (currentPos + i) % PokerConstants.MAX_PLAYERS;
            address playerAddr = stateStorage.getPlayerAtPosition(candidatePos);
            
            if (playerAddr != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(playerAddr);
                if (player.status == IStateStorage.PlayerStatus.Active) {
                    tournament.buttonPosition = candidatePos;
                    stateStorage.updateTournamentState(tournament);
                    emit ButtonRotated(candidatePos);
                    return;
                }
            }
        }
        
        revert('No active players found for button position');
    }
    
    /**
     * @notice Check and update blinds if needed (automatically)
     * @dev This function can be called from HandManager at the start of a new hand
     */
    function checkAndUpdateBlinds() external override {
        IStateStorage.TournamentState memory tournament = stateStorage
            .getTournamentState();
            
        // Only proceed if tournament is active and not paused
        if (tournament.tableState != IStateStorage.TableState.Active || tournament.isPaused) {
            return; // Silent return - just a check
        }

        uint256 currentLevel = getCurrentBlindLevel();
        uint256 expectedLevel = getExpectedBlindLevel();
        
        // Only update if we need to increase level
        if (expectedLevel > currentLevel) {
            // Calculate how many levels to increase at once (for cases where multiple levels were missed)
            uint256 levelIncrease = expectedLevel - currentLevel;
            
            // Get the new blinds based on the expected level
            (uint256 newSmallBlind, uint256 newBigBlind) = calculateNextBlinds(
                expectedLevel
            );

            // Essential tournament rule: cap maximum blinds
            if (newSmallBlind > MAX_SMALL_BLIND) {
                newSmallBlind = MAX_SMALL_BLIND;
                newBigBlind = MAX_SMALL_BLIND * 2;
            }

            // Update tournament state
            tournament.smallBlind = newSmallBlind;
            tournament.bigBlind = newBigBlind;
            tournament.lastBlindUpdate = block.timestamp;
            tournament.currentBlindLevel = expectedLevel;

            // Add new blind level to history
            IStateStorage.BlindLevel memory newLevel = IStateStorage
                .BlindLevel({
                    smallBlind: newSmallBlind,
                    bigBlind: newBigBlind,
                    startTime: block.timestamp
                });
            stateStorage.addBlindLevel(newLevel);

            // Emit event for blind increase with level information
            emit BlindsIncreased(newSmallBlind, newBigBlind);
            emit TournamentBlindsUpdated(
                newSmallBlind,
                newBigBlind,
                expectedLevel,
                block.timestamp
            );

            // Essential tournament rule: end tournament if blinds too high relative to stacks
            uint256 avgStack = _calculateAverageStack();
            if (tournament.smallBlind > avgStack / 4) {
                // Use current small blind
                _completeTournament();
                return;
            }

            stateStorage.updateTournamentState(tournament);
        }
    }
}
