// SPDX-License-Identifier: MIT
pragma solidity ^0.8.18;

library PokerConstants {
    // Hand rankings
    uint16 constant ROYAL_FLUSH = 1;
    uint16 constant STRAIGHT_FLUSH = 2;
    uint16 constant FOUR_OF_A_KIND = 3;
    uint16 constant FULL_HOUSE = 4;
    uint16 constant FLUSH = 5;
    uint16 constant STRAIGHT = 6;
    uint16 constant THREE_OF_A_KIND = 7;
    uint16 constant TWO_PAIR = 8;
    uint16 constant ONE_PAIR = 9;
    uint16 constant HIGH_CARD = 10;

    // Card values
    uint8 constant TWO = 0;
    uint8 constant THREE = 1;
    uint8 constant FOUR = 2;
    uint8 constant FIVE = 3;
    uint8 constant SIX = 4;
    uint8 constant SEVEN = 5;
    uint8 constant EIGHT = 6;
    uint8 constant NINE = 7;
    uint8 constant TEN = 8;
    uint8 constant JACK = 9;
    uint8 constant QUEEN = 10;
    uint8 constant KING = 11;
    uint8 constant ACE = 12;

    // Suits
    uint8 constant HEARTS = 0;
    uint8 constant DIAMONDS = 1;
    uint8 constant CLUBS = 2;
    uint8 constant SPADES = 3;

    // Game constants
    uint8 constant DECK_SIZE = 52;
    uint8 constant HAND_SIZE = 2;
    uint8 constant COMMUNITY_CARDS = 5;
    uint8 constant MAX_PLAYERS = 5;
    uint256 constant ACTION_TIMEOUT = 30 seconds;
    uint256 constant BLIND_LEVEL_DURATION = 5 minutes;
}

library DeckManager {
    using PokerConstants for uint8;

    struct Deck {
        uint8[52] cards;
        uint8 currentIndex;
        bytes32 lastSeed;
    }

    event ShuffleInitiated(bytes32 seed);
    event CardDealt(uint8 indexed position, uint8 cardIndex);

    function initializeDeck() internal pure returns (Deck memory) {
        Deck memory deck;
        for (uint8 i = 0; i < PokerConstants.DECK_SIZE; i++) {
            deck.cards[i] = i;
        }
        deck.currentIndex = 0;
        return deck;
    }

    function shuffle(Deck storage deck) internal {
        // Use TEN's block.difficulty as secure RNG source
        // TODO: ZIGA - double check if we should use block.prevrandao or block.difficulty in TEN and what is the difference.
        bytes32 seed = bytes32(block.prevrandao);
        deck.lastSeed = seed;
        emit ShuffleInitiated(seed);

        for (uint8 i = PokerConstants.DECK_SIZE - 1; i > 0; i--) {
            // Use seed to generate random index
            uint8 j = uint8(
                uint256(keccak256(abi.encodePacked(seed, i))) % (i + 1)
            );
            // Swap cards
            (deck.cards[i], deck.cards[j]) = (deck.cards[j], deck.cards[i]);
        }
        deck.currentIndex = 0;
    }

    function dealCard(
        Deck storage deck,
        uint8 position
    ) internal returns (uint8) {
        require(
            deck.currentIndex < PokerConstants.DECK_SIZE,
            'No cards left in deck'
        );
        uint8 card = deck.cards[deck.currentIndex];
        deck.currentIndex++;
        emit CardDealt(position, card);
        return card;
    }

    function dealHoleCards(
        Deck storage deck,
        uint8 playerPosition
    ) internal returns (uint8[2] memory) {
        uint8[2] memory cards;
        cards[0] = dealCard(deck, playerPosition);
        cards[1] = dealCard(deck, playerPosition);
        return cards;
    }

    function dealCommunityCards(
        Deck storage deck,
        uint8 count
    ) internal returns (uint8[] memory) {
        uint8[] memory cards = new uint8[](count);
        for (uint8 i = 0; i < count; i++) {
            cards[i] = dealCard(deck, type(uint8).max); // max value indicates community card
        }
        return cards;
    }

    function getCardValue(uint8 card) internal pure returns (uint8) {
        return card % 13;
    }

    function getCardSuit(uint8 card) internal pure returns (uint8) {
        return card / 13;
    }
}

library SecurityManager {
    using PokerConstants for uint8;

    event CardRevealRequested(address indexed player, uint8 position);
    event HandRevealed(address indexed player, uint8[2] holeCards);

    // Mapping to track card visibility permissions
    struct CardVisibility {
        mapping(address => mapping(uint8 => bool)) canSeeCard;
        mapping(uint8 => bool) isRevealed;
    }

    function grantCardVisibility(
        CardVisibility storage visibility,
        address player,
        uint8 cardPosition
    ) internal {
        visibility.canSeeCard[player][cardPosition] = true;
    }

    function revokeCardVisibility(
        CardVisibility storage visibility,
        address player,
        uint8 cardPosition
    ) internal {
        visibility.canSeeCard[player][cardPosition] = false;
    }

    function canSeeCard(
        CardVisibility storage visibility,
        address player,
        uint8 cardPosition
    ) internal view returns (bool) {
        return
            visibility.canSeeCard[player][cardPosition] ||
            visibility.isRevealed[cardPosition];
    }

    function revealHand(
        CardVisibility storage visibility,
        address player,
        uint8[2] memory holeCards
    ) internal {
        visibility.isRevealed[holeCards[0]] = true;
        visibility.isRevealed[holeCards[1]] = true;
        emit HandRevealed(player, holeCards);
    }

    function requestCardReveal(
        CardVisibility storage visibility,
        address player,
        uint8 position
    ) internal {
        require(!visibility.isRevealed[position], 'Card already revealed');
        emit CardRevealRequested(player, position);
    }
}
