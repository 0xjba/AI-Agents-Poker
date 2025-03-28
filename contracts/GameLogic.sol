// SPDX-License-Identifier: MIT
pragma solidity ^0.8.18;

import './library.sol';
import './interfaces.sol';
import './HandManager.sol';
import './HandEvaluator.sol';

contract GameLogic is IGameLogic {
    bool private locked;
    using PokerConstants for uint8;

    IStateStorage private stateStorage;
    HandManager private handManager;
    HandEvaluator private handEvaluator;

    IRouter private router;
    bool private routerSet = false;

    uint8 private constant FOLD = 0;
    uint8 private constant CHECK = 1;
    uint8 private constant CALL = 2;
    uint8 private constant RAISE = 3;

    struct WinnerInfo {
        address singleWinner;
        address[] tiedWinners;
        bool isTied;
    }

    event ButtonRotated();
    event NewHandStarted();

    modifier synchronized() {require(!locked, 'Reentrant'); locked = true; _; locked = false;}

    constructor(address _stateStorage, address _handManager, address _handEvaluator) {
        stateStorage = IStateStorage(_stateStorage);
        handManager = HandManager(_handManager);
        handEvaluator = HandEvaluator(_handEvaluator);
    }

    function setRouter(address _r) external {
        require(!routerSet, 'Router set');
        require(_r != address(0), 'Invalid addr');
        router = IRouter(_r);
        routerSet = true;
    }

    function processAction(address player, uint8 action, uint256 amount) external override synchronized {
        IStateStorage.GameState memory gameState = stateStorage.getGameState();
        
        require(gameState.currentTurn != address(0), 'Invalid game state');
        require(player == gameState.currentTurn, 'Not your turn');
        
        IStateStorage.Player memory playerState = stateStorage.getPlayer(player);
        
        if (playerState.stack == 0) {
            if (playerState.status != IStateStorage.PlayerStatus.AllIn) {
                playerState.status = IStateStorage.PlayerStatus.AllIn;
                stateStorage.updatePlayerState(player, playerState);
            }
            _updateGameState();
            return;
        }
        
        if (gameState.currentBet > playerState.currentBet && 
            playerState.stack <= (gameState.currentBet - playerState.currentBet) &&
            (action == CALL || action == RAISE)) {
                
            uint256 remainingChips = playerState.stack;
            playerState.currentBet += remainingChips;
            playerState.stack = 0;
            playerState.status = IStateStorage.PlayerStatus.AllIn;
            playerState.totalContribution += remainingChips;
            
            gameState.mainPot += remainingChips;
            
            stateStorage.updatePlayerState(player, playerState);
            stateStorage.updateGameState(gameState);
            stateStorage.setPlayerActedInRound(player, true);
            
            emit ActionTaken(player, action, remainingChips);
            
            _updateGameState();
            return;
        }

        emit ActionTaken(player, action, amount);

        if (action == FOLD) _processFold(player);
        else if (action == CHECK) _processCheck(player);
        else if (action == CALL) _processCall(player);
        else if (action == RAISE) _processRaise(player, amount);
        else revert('Invalid action');
    }

    function handlePlayerTimeout(address player) external override {
        IStateStorage.GameState memory gameState = stateStorage.getGameState();
        require(player == gameState.currentTurn, "Not current player's turn");
        require(!stateStorage.getTournamentState().isPaused, 'Game is paused');
        
        (uint8 activeCount, uint8 allInCount) = _getPlayerStatusCounts();
        if (activeCount == 0 && allInCount >= 2) {
            _handleShowdown();
            return;
        } else if (activeCount == 0 && allInCount == 1) {
            _awardPotToLastPlayer();
            return;
        }
        
        IStateStorage.Player memory playerState = stateStorage.getPlayer(player);
        
        if (playerState.stack == 0) {
            if (playerState.status != IStateStorage.PlayerStatus.AllIn) {
                playerState.status = IStateStorage.PlayerStatus.AllIn;
                stateStorage.updatePlayerState(player, playerState);
            }
            
            stateStorage.setPlayerActedInRound(player, true);
            
            emit PlayerTimedOut(player);
            _updateGameState();
            return;
        } 
        else if (gameState.currentBet > playerState.currentBet && playerState.stack <= (gameState.currentBet - playerState.currentBet)) {
            uint256 remainingChips = playerState.stack;
            playerState.currentBet += remainingChips;
            playerState.stack = 0;
            playerState.status = IStateStorage.PlayerStatus.AllIn;
            playerState.totalContribution += remainingChips;
            
            gameState.mainPot += remainingChips;
            
            stateStorage.updatePlayerState(player, playerState);
            stateStorage.updateGameState(gameState);
            stateStorage.setPlayerActedInRound(player, true);
            
            emit PlayerTimedOut(player);
            
            _updateGameState();
            return;
        } 
        else {
            _processFold(player);
            emit PlayerTimedOut(player);
            _updateGameState();
            return;
        }
    }

    function nextRound() external override synchronized {_nextRound();}

    function getValidActions(address player) external view override returns (bool[] memory) {
        IStateStorage.GameState memory gs = stateStorage.getGameState();
        IStateStorage.Player memory ps = stateStorage.getPlayer(player);
        bool[] memory va = new bool[](4);

        if (ps.stack == 0 && ps.status == IStateStorage.PlayerStatus.Active || ps.status == IStateStorage.PlayerStatus.AllIn) return va;
        
        va[FOLD] = true;
        va[CHECK] = (gs.currentBet == 0 || ps.currentBet == gs.currentBet);
        va[CALL] = ps.stack > 0;
        va[RAISE] = (ps.stack >= gs.currentBet * 2);

        return va;
    }

    function _processFold(address player) private {
        IStateStorage.Player memory ps = stateStorage.getPlayer(player);
        ps.status = IStateStorage.PlayerStatus.Folded;
        ps.totalContribution += ps.currentBet;
        stateStorage.updatePlayerState(player, ps);
        _moveToNextPlayer();
    }

    function _processCheck(address player) private {
        IStateStorage.GameState memory gs = stateStorage.getGameState();
        IStateStorage.Player memory ps = stateStorage.getPlayer(player);
        require(gs.currentBet == 0 || ps.currentBet == gs.currentBet, 'No check');
        stateStorage.setPlayerActedInRound(player, true);
        _moveToNextPlayer();
    }

    function _processCall(address player) private {
        IStateStorage.GameState memory gs = stateStorage.getGameState();
        IStateStorage.Player memory ps = stateStorage.getPlayer(player);
        uint256 callAmount = gs.currentBet - ps.currentBet;

        if (callAmount >= ps.stack) {
            ps.status = IStateStorage.PlayerStatus.AllIn;
            uint256 totalChips = ps.currentBet + ps.stack;
            ps.currentBet = totalChips;
            if (totalChips > gs.currentBet) gs.currentBet = totalChips;
            gs.mainPot += ps.stack;
            ps.totalContribution += ps.stack;
            ps.stack = 0;
            stateStorage.updatePlayerState(player, ps);
            stateStorage.updateGameState(gs);
            stateStorage.setPlayerActedInRound(player, true);
            _moveToNextPlayer();
            return;
        }

        require(ps.stack >= callAmount, 'Low chips');
        ps.stack -= callAmount;
        ps.currentBet = gs.currentBet;
        gs.mainPot += callAmount;
        ps.totalContribution += callAmount;
        gs.lastActionAmount = callAmount;
        stateStorage.updatePlayerState(player, ps);
        stateStorage.updateGameState(gs);
        stateStorage.setPlayerActedInRound(player, true);
        _moveToNextPlayer();
    }

    function _processRaise(address player, uint256 raiseAmount) private {
        IStateStorage.GameState memory gs = stateStorage.getGameState();
        IStateStorage.Player memory ps = stateStorage.getPlayer(player);
        IStateStorage.TournamentState memory t = stateStorage.getTournamentState();

        require(raiseAmount > 0, 'Raise 0');
        uint256 toCall = gs.currentBet > ps.currentBet ? gs.currentBet - ps.currentBet : 0;
        require(raiseAmount <= type(uint256).max - toCall, 'Raise too big');
        uint256 totalAmount = toCall + raiseAmount;

        if (totalAmount == ps.stack) {
            ps.status = IStateStorage.PlayerStatus.AllIn;
            uint256 totalBet = ps.currentBet + ps.stack;
            gs.currentBet = totalBet;
            gs.mainPot += ps.stack;
            ps.totalContribution += ps.stack;
            ps.currentBet = totalBet;
            ps.stack = 0;
            stateStorage.updatePlayerState(player, ps);
            stateStorage.updateGameState(gs);
            stateStorage.setPlayerActedInRound(player, true);
            _moveToNextPlayer();
            return;
        }

        uint256 minRaiseAmount = gs.lastRaise > 0 ? gs.lastRaise : t.bigBlind;
        require(raiseAmount >= minRaiseAmount, 'Raise small');
        require(ps.stack >= totalAmount, 'Low chips');

        ps.stack -= totalAmount;
        gs.mainPot += totalAmount;
        ps.currentBet = gs.currentBet + raiseAmount;
        gs.currentBet = ps.currentBet;
        gs.lastRaise = raiseAmount;
        gs.lastActionAmount = totalAmount;
        gs.lastAggressor = ps.position;
        ps.totalContribution += totalAmount;
        stateStorage.setPlayerActedInRound(player, true);

        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address otherP = stateStorage.getPlayerAtPosition(i);
            if (otherP != address(0) && otherP != player) {
                IStateStorage.Player memory opState = stateStorage.getPlayer(otherP);
                if (opState.status == IStateStorage.PlayerStatus.Active) {
                    stateStorage.setPlayerActedInRound(otherP, false);
                }
            }
        }

        stateStorage.updatePlayerState(player, ps);
        stateStorage.updateGameState(gs);
        _moveToNextPlayer();
    }

    function _nextRound() private {
        IStateStorage.GameState memory gameState = stateStorage.getGameState();

        require(
            gameState.currentRound < IStateStorage.BettingRound.River,
            'Hand complete'
        );

        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address playerAddr = stateStorage.getPlayerAtPosition(i);
            if (playerAddr != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(
                    playerAddr
                );
                
                if (player.stack == 0 && player.status == IStateStorage.PlayerStatus.Active) {
                    player.status = IStateStorage.PlayerStatus.AllIn;
                }
                
                if (player.status != IStateStorage.PlayerStatus.Eliminated) {
                    if (player.currentBet > 0) {
                        player.totalContribution += player.currentBet;
                        player.currentBet = 0;
                    }
                    
                    stateStorage.updatePlayerState(playerAddr, player);
                    
                    if (player.status == IStateStorage.PlayerStatus.Active) {
                        stateStorage.setPlayerActedInRound(playerAddr, false);
                    }
                }
            }
        }

        gameState.currentBet = 0;
        gameState.lastRaise = 0;

        (uint8 activeCount, ) = _getPlayerStatusCounts();

        if (activeCount > 0) {
            address sbPlayer = stateStorage.getPlayerAtPosition(1);
            if (gameState.currentRound >= IStateStorage.BettingRound.PreFlop) {
                gameState.currentTurn = sbPlayer != address(0)
                    ? _getNextActivePlayer(stateStorage.getPlayerAtPosition(0))
                    : _getNextActivePlayer(address(0));
            } else {
                gameState.currentTurn = _getNextActivePlayer(address(0));
            }
        } else {
            gameState.currentTurn = address(0);
        }

        uint8[] memory newCards;
        if (gameState.currentRound == IStateStorage.BettingRound.PreFlop) {
            newCards = handManager.dealFlop();
            gameState.communityCards[0] = newCards[0];
            gameState.communityCards[1] = newCards[1];
            gameState.communityCards[2] = newCards[2];
            gameState.currentRound = IStateStorage.BettingRound.Flop;
        } else if (gameState.currentRound == IStateStorage.BettingRound.Flop) {
            newCards = handManager.dealTurn();
            gameState.communityCards[3] = newCards[0];
            gameState.currentRound = IStateStorage.BettingRound.Turn;
        } else if (gameState.currentRound == IStateStorage.BettingRound.Turn) {
            newCards = handManager.dealRiver();
            gameState.communityCards[4] = newCards[0];
            gameState.currentRound = IStateStorage.BettingRound.River;
        }

        stateStorage.updateGameState(gameState);
        emit RoundStarted(gameState.currentRound);
    }

    function _moveToNextPlayer() private {
        (uint8 activeC, uint8 allInC) = _getPlayerStatusCounts();
        if (activeC == 1 && allInC == 0) {_awardPotToLastPlayer(); return;}

        IStateStorage.GameState memory gs = stateStorage.getGameState();
        bool roundComplete = _isRoundComplete();

        if (roundComplete) {
            if (_shouldShowdown()) {
                if (gs.currentRound < IStateStorage.BettingRound.River) {
                    while (gs.currentRound < IStateStorage.BettingRound.River) {
                        _nextRound();
                        gs = stateStorage.getGameState();
                    }
                }
                _handleShowdown();
            } else _nextRound();
        } else {
            IStateStorage.Player memory currPlayer = stateStorage.getPlayer(gs.currentTurn);
            if (currPlayer.stack == 0 && currPlayer.status == IStateStorage.PlayerStatus.Active) {
                currPlayer.status = IStateStorage.PlayerStatus.AllIn;
                stateStorage.updatePlayerState(gs.currentTurn, currPlayer);
                _updateGameState();
                return;
            }
            
            address nextP = _getNextActivePlayer(gs.currentTurn);
            if (nextP == address(0)) {
                (activeC, allInC) = _getPlayerStatusCounts();
                if (allInC >= 2 || (activeC == 0 && allInC > 0)) {_handleShowdown(); return;} 
                else {_awardPotToLastPlayer(); return;}
            }
            
            gs.currentTurn = nextP;
            stateStorage.updateGameState(gs);
            
            IStateStorage.TournamentState memory t = stateStorage.getTournamentState();
            if (!t.isPaused && nextP != address(0)) emit ActionTimerStarted(nextP, gs.actionTimer, block.number);
        }
    }

    function _updateGameState() private {
        IStateStorage.GameState memory gameState = stateStorage.getGameState();

        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address playerAddr = stateStorage.getPlayerAtPosition(i);
            if (playerAddr != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(playerAddr);
                if (player.stack == 0 && player.status == IStateStorage.PlayerStatus.Active) {
                    player.status = IStateStorage.PlayerStatus.AllIn;
                    stateStorage.updatePlayerState(playerAddr, player);
                }
            }
        }

        (uint8 activeCount, uint8 allInCount) = _getPlayerStatusCounts();

        if (activeCount == 0 && allInCount >= 2) {
            _handleShowdown();
            return;
        }
        if (activeCount == 0 && allInCount == 1) {
            _awardPotToLastPlayer();
            return;
        }

        if (_isRoundComplete()) {
            if (_shouldShowdown()) {
                _handleShowdown();
            } else {
                emit RoundComplete(gameState.currentRound);
            }
        } else {
            address nextPlayer = _getNextActivePlayer(gameState.currentTurn);
            if (nextPlayer == address(0)) {
                (activeCount, allInCount) = _getPlayerStatusCounts();
                
                if (allInCount >= 2 || (activeCount == 0 && allInCount > 0)) {
                    _handleShowdown();
                    return;
                } else {
                    _awardPotToLastPlayer();
                    return;
                }
            }
            
            gameState.currentTurn = nextPlayer;
            stateStorage.updateGameState(gameState);

            emit ActionTimerStarted(
                nextPlayer,
                gameState.actionTimer,
                block.number
            );
        }
    }

    struct Pot {
        uint256 amount;
        address[] eligiblePlayers;
    }

    function _distributePots() private {
        IStateStorage.GameState memory gameState = stateStorage.getGameState();
        uint256 originalPot = gameState.mainPot;
        
        address[] memory playerAddresses = new address[](PokerConstants.MAX_PLAYERS);
        uint256[] memory contributions = new uint256[](PokerConstants.MAX_PLAYERS);
        uint8 playerCount = 0;
        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address playerAddr = stateStorage.getPlayerAtPosition(i);
            if (playerAddr != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(
                    playerAddr
                );
                if (
                    player.status == IStateStorage.PlayerStatus.Active ||
                    player.status == IStateStorage.PlayerStatus.AllIn
                ) {
                    playerAddresses[playerCount] = playerAddr;
                    contributions[playerCount] = player.totalContribution;
                    playerCount++;
                }
            }
        }
        if (playerCount == 1) {
            IStateStorage.Player memory winner = stateStorage.getPlayer(
                playerAddresses[0]
            );
            
            winner.stack += originalPot;
            stateStorage.updatePlayerState(playerAddresses[0], winner);
            
            gameState.mainPot = 0;
            gameState.lastActionAmount = type(uint256).max;
            stateStorage.updateGameState(gameState);
            
            return;
        }
        for (uint8 i = 0; i < playerCount - 1; i++) {
            for (uint8 j = 0; j < playerCount - i - 1; j++) {
                if (contributions[j] > contributions[j + 1]) {
                    uint256 tempContribution = contributions[j];
                    contributions[j] = contributions[j + 1];
                    contributions[j + 1] = tempContribution;
                    address tempPlayer = playerAddresses[j];
                    playerAddresses[j] = playerAddresses[j + 1];
                    playerAddresses[j + 1] = tempPlayer;
                }
            }
        }
        uint256[] memory uniqueContributions = new uint256[](playerCount);
        uint8 uniqueCount = 0;
        for (uint8 i = 0; i < playerCount; i++) {
            if (i == 0 || contributions[i] > contributions[i - 1]) {
                uniqueContributions[uniqueCount] = contributions[i];
                uniqueCount++;
            }
        }
        Pot[] memory pots = new Pot[](uniqueCount);
        uint256 prevThreshold = 0;
        for (uint8 i = 0; i < uniqueCount; i++) {
            uint256 currentThreshold = uniqueContributions[i];
            uint8 eligibleCount = 0;
            for (uint8 j = 0; j < playerCount; j++) {
                if (contributions[j] >= currentThreshold) {
                    eligibleCount++;
                }
            }
            address[] memory eligiblePlayers = new address[](eligibleCount);
            uint8 eligibleIndex = 0;
            uint256 potAmount = 0;
            for (uint8 j = 0; j < playerCount; j++) {
                if (contributions[j] >= currentThreshold) {
                    eligiblePlayers[eligibleIndex] = playerAddresses[j];
                    eligibleIndex++;
                    potAmount += (currentThreshold - prevThreshold);
                }
            }
            for (uint8 j = 0; j < PokerConstants.MAX_PLAYERS; j++) {
                address playerAddr = stateStorage.getPlayerAtPosition(j);
                if (playerAddr != address(0)) {
                    IStateStorage.Player memory player = stateStorage.getPlayer(
                        playerAddr
                    );
                    if (
                        player.status == IStateStorage.PlayerStatus.Folded &&
                        player.totalContribution > prevThreshold
                    ) {
                        uint256 contribution = player.totalContribution >=
                            currentThreshold
                            ? currentThreshold - prevThreshold
                            : player.totalContribution - prevThreshold;
                        potAmount += contribution;
                    }
                }
            }
            pots[i] = Pot({
                amount: potAmount,
                eligiblePlayers: eligiblePlayers
            });
            prevThreshold = currentThreshold;
        }
        uint256 totalPotsAmount = 0;
        for (uint8 i = 0; i < uniqueCount; i++) {
            totalPotsAmount += pots[i].amount;
        }
        
        // If we have any pots, adjust the last one to match the total rather than failing
        if (totalPotsAmount != originalPot && uniqueCount > 0) {
            pots[uniqueCount-1].amount += (originalPot - totalPotsAmount);
        }
        
        for (uint8 i = 0; i < uniqueCount; i++) {
            _awardPotToWinners(pots[i]);
        }
        
        gameState.mainPot = 0;
        stateStorage.updateGameState(gameState);
    }

    function _awardPotToWinners(Pot memory pot) private {
        if (pot.amount == 0 || pot.eligiblePlayers.length == 0) return;

        if (pot.eligiblePlayers.length == 1) {
            address winner = pot.eligiblePlayers[0];
            IStateStorage.Player memory ws = stateStorage.getPlayer(winner);
            ws.stack += pot.amount;
            stateStorage.updatePlayerState(winner, ws);
            return;
        }

        uint8 bestHandType = 10;
        uint32 bestHandRank = 0;
        address[] memory winners = new address[](pot.eligiblePlayers.length);
        uint8 winnerCount = 0;
        IStateStorage.GameState memory gs = stateStorage.getGameState();

        for (uint8 i = 0; i < pot.eligiblePlayers.length; i++) {
            address playerAddr = pot.eligiblePlayers[i];
            IStateStorage.Player memory player = stateStorage.getPlayer(playerAddr);
            (uint32 handRank, uint8 handType) = handEvaluator.evaluateHoldemHand(player.holeCards, gs.communityCards);

            if (handType < bestHandType) {
                bestHandType = handType;
                bestHandRank = handRank;
                winnerCount = 1;
                winners[0] = playerAddr;
            } else if (handType == bestHandType) {
                if (handRank > bestHandRank) {
                    bestHandRank = handRank;
                    winnerCount = 1;
                    winners[0] = playerAddr;
                } else if (handRank == bestHandRank) {
                    winners[winnerCount] = playerAddr;
                    winnerCount++;
                }
            }
        }

        uint256 amountPerWinner = pot.amount / winnerCount;
        uint256 remainder = pot.amount % winnerCount;

        for (uint8 i = 0; i < winnerCount; i++) {
            address winner = winners[i];
            IStateStorage.Player memory ws = stateStorage.getPlayer(winner);
            ws.stack += (i == winnerCount - 1) ? amountPerWinner + remainder : amountPerWinner;
            stateStorage.updatePlayerState(winner, ws);
        }
    }

    function _awardPotToLastPlayer() private {
        uint8 rPlayers = 0;
        address lPlayer = address(0);
        
        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address pAddr = stateStorage.getPlayerAtPosition(i);
            if (pAddr != address(0)) {
                IStateStorage.Player memory p = stateStorage.getPlayer(pAddr);
                if (p.status == IStateStorage.PlayerStatus.Active || p.status == IStateStorage.PlayerStatus.AllIn) {
                    rPlayers++;
                    lPlayer = pAddr;
                }
            }
        }
        
        if (rPlayers > 1) require(false, 'Too many players');
        if (lPlayer == address(0)) require(false, 'No players left');

        IStateStorage.GameState memory gs = stateStorage.getGameState();
        IStateStorage.Player memory w = stateStorage.getPlayer(lPlayer);
        uint256 pot = gs.mainPot;
        w.stack += pot;
        stateStorage.updatePlayerState(lPlayer, w);
        
        gs.mainPot = 0;
        gs.lastActionAmount = type(uint256).max;
        stateStorage.updateGameState(gs);
        _resetGameState();
    }

    function _getNextActivePlayer(address cp) private view returns (address) {
        IStateStorage.TournamentState memory t = stateStorage.getTournamentState();
        uint8 currPos = cp == address(0) ? t.buttonPosition : stateStorage.getPlayer(cp).position;

        for (uint8 i = 1; i <= PokerConstants.MAX_PLAYERS; i++) {
            uint8 nextPos = (currPos + i) % PokerConstants.MAX_PLAYERS;
            address pAtPos = stateStorage.getPlayerAtPosition(nextPos);

            if (pAtPos != address(0)) {
                IStateStorage.Player memory p = stateStorage.getPlayer(pAtPos);
                if (p.stack == 0) {
                    if (p.status == IStateStorage.PlayerStatus.Active) continue;
                } else if (p.status == IStateStorage.PlayerStatus.Active) return pAtPos;
            }
        }
        return address(0);
    }

    function _getActivePlayerCount() private view returns (uint8 count) {
        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address pa = stateStorage.getPlayerAtPosition(i);
            if (pa != address(0)) {
                IStateStorage.Player memory p = stateStorage.getPlayer(pa);
                if (p.status == IStateStorage.PlayerStatus.Active && p.stack > 0) count++;
            }
        }
    }

    function _getPlayerStatusCounts() private view returns (uint8 activeCount, uint8 allInCount) {
        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address playerAddress = stateStorage.getPlayerAtPosition(i);
            if (playerAddress != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(playerAddress);
                if (player.status == IStateStorage.PlayerStatus.Active) {if (player.stack > 0) activeCount++; else allInCount++;} 
                else if (player.status == IStateStorage.PlayerStatus.AllIn) allInCount++;
            }
        }
    }

    function _isRoundComplete() private view returns (bool) {
        IStateStorage.GameState memory gs = stateStorage.getGameState();
        if (gs.currentRound == IStateStorage.BettingRound.PreFlop) {
            address bbP = stateStorage.getPlayerAtPosition(2);
            if (bbP != address(0)) {
                IStateStorage.Player memory bbPS = stateStorage.getPlayer(bbP);
                if (bbPS.currentBet == gs.currentBet && bbPS.status == IStateStorage.PlayerStatus.Active && bbPS.stack > 0 && !stateStorage.hasPlayerActedInRound(bbP)) return false;
            }
        }

        uint8 activeC;
        uint8 actedC;
        uint8 allInC;

        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address pAddr = stateStorage.getPlayerAtPosition(i);
            if (pAddr != address(0)) {
                IStateStorage.Player memory p = stateStorage.getPlayer(pAddr);
                if (p.status == IStateStorage.PlayerStatus.Active) {
                    if (p.stack > 0) {
                        activeC++;
                        if (stateStorage.hasPlayerActedInRound(pAddr)) actedC++;
                    } else allInC++;
                } else if (p.status == IStateStorage.PlayerStatus.AllIn) allInC++;
            }
        }

        return ((activeC > 0) && (activeC == actedC)) || ((activeC == 0) && (allInC > 0));
    }

    function _shouldShowdown() private view returns (bool) {
        (uint8 a, uint8 ai) = _getPlayerStatusCounts();
        return stateStorage.getGameState().currentRound == IStateStorage.BettingRound.River || (a == 0 && ai >= 2) || (a == 1 && ai >= 1);
    }

    function _handleShowdown() private {
        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address pAddr = stateStorage.getPlayerAtPosition(i);
            if (pAddr != address(0)) {
                IStateStorage.Player memory p = stateStorage.getPlayer(pAddr);
                // Ensure consistent handling of players with zero stack
                if (p.stack == 0 && p.status == IStateStorage.PlayerStatus.Active) {
                    p.status = IStateStorage.PlayerStatus.AllIn;
                    stateStorage.updatePlayerState(pAddr, p);
                }
                if (p.status == IStateStorage.PlayerStatus.Active || p.status == IStateStorage.PlayerStatus.AllIn) {
                    handManager.revealHand(pAddr);
                }
            }
        }

        IStateStorage.GameState memory gs = stateStorage.getGameState();
        stateStorage.setPreviousCommunityCards(gs.communityCards);
        
        // Regular pot distribution
        _distributePots();

        gs = stateStorage.getGameState();
        gs.lastActionAmount = type(uint256).max;
        stateStorage.updateGameState(gs);
        _resetGameState();
    }

    function _resetGameState() private {
        IStateStorage.GameState memory gameState = stateStorage.getGameState();

        gameState.currentRound = IStateStorage.BettingRound.PreFlop;
        gameState.currentBet = 0;
        gameState.lastRaise = 0;
        gameState.minRaise = 0;
        gameState.lastAggressor = 0;
        gameState.lastActionAmount = 0;

        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address playerAddress = stateStorage.getPlayerAtPosition(i);
            if (playerAddress != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(
                    playerAddress
                );
                player.holeCards = [0, 0];

                if (player.stack == 0) player.status = IStateStorage.PlayerStatus.Eliminated;
                else if (player.status != IStateStorage.PlayerStatus.Active && 
                         player.status != IStateStorage.PlayerStatus.Eliminated)
                    player.status = IStateStorage.PlayerStatus.Active;

                player.currentBet = 0;
                player.totalContribution = 0;
                stateStorage.updatePlayerState(playerAddress, player);
            }
        }

        _checkPlayerEliminations();

        uint8[5] memory emptyCards = [0, 0, 0, 0, 0];
        gameState.communityCards = emptyCards;

        address nextPlayer = _getNextActivePlayer(address(0));
        
        if (nextPlayer == address(0)) {
            uint8 remainingPlayers = 0;
            address lastPlayer = address(0);
            
            for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
                address playerAddr = stateStorage.getPlayerAtPosition(i);
                if (playerAddr != address(0)) {
                    IStateStorage.Player memory player = stateStorage.getPlayer(playerAddr);
                    if (player.status != IStateStorage.PlayerStatus.Eliminated) {
                        remainingPlayers++;
                        lastPlayer = playerAddr;
                    }
                }
            }
            
            if (remainingPlayers == 1 && lastPlayer != address(0)) {
                gameState.currentTurn = address(0);
                if (routerSet) try router.eliminatePlayer(lastPlayer) {} catch {}
            } else {
                for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
                    address playerAddr = stateStorage.getPlayerAtPosition(i);
                    if (playerAddr != address(0)) {
                        IStateStorage.Player memory p = stateStorage.getPlayer(playerAddr);
                        if (p.stack == 0 && p.status == IStateStorage.PlayerStatus.Active) {
                            p.status = IStateStorage.PlayerStatus.AllIn;
                            stateStorage.updatePlayerState(playerAddr, p);
                        }
                    }
                }
                nextPlayer = _getNextActivePlayer(address(0));
                gameState.currentTurn = nextPlayer;
            }
        } else {
            gameState.currentTurn = nextPlayer;
        }

        stateStorage.updateGameState(gameState);

        gameState.handStartTime = block.timestamp;

        stateStorage.resetPlayerActions();

        IStateStorage.TournamentState memory tournament = stateStorage.getTournamentState();
        if (routerSet) try IRouter(address(router)).updateButtonPosition() {emit ButtonRotated();} catch {}

        if (
            tournament.tableState == IStateStorage.TableState.Active &&
            tournament.activePlayerCount >= 2 &&
            !tournament.isPaused
        ) {
            if (routerSet) try router.startNewHand() {emit NewHandStarted();} catch {try router.startNewHand() {emit NewHandStarted();} catch {}}
        }
    }

    address private pokerBettingContract;
    
    function setPokerBettingContract(address _pbc) external {
        require(routerSet && IRouter(router).isAdmin(msg.sender), "Not auth");
        require(_pbc != address(0), 'Invalid');
        pokerBettingContract = _pbc;
    }
    
    function _checkPlayerEliminations() private {
        for (uint8 i = 0; i < PokerConstants.MAX_PLAYERS; i++) {
            address playerAddr = stateStorage.getPlayerAtPosition(i);
            if (playerAddr != address(0)) {
                IStateStorage.Player memory player = stateStorage.getPlayer(playerAddr);
                if (player.stack == 0 && player.status != IStateStorage.PlayerStatus.Eliminated) {
                    player.status = IStateStorage.PlayerStatus.Eliminated;
                    stateStorage.updatePlayerState(playerAddr, player);
                    if (routerSet) {
                        try router.eliminatePlayer(playerAddr) {
                            if (pokerBettingContract != address(0)) {
                                uint256 tournamentId = 1;
                                (bool success, ) = pokerBettingContract.call(abi.encodeWithSignature('handlePlayerElimination(uint256,address)', tournamentId, playerAddr));
                            }
                        } catch {}
                    }
                }
            }
        }
    }
}