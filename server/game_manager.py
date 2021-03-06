from binascii import hexlify
from collections import deque
from itertools import count
from os import urandom
from server.exceptions import *
from settings.game_settings import *
from settings.server_settings import *
from threading import Thread, Timer, Event
import json
import logging
import sqlite3

logger = logging.getLogger(__name__)

class Manager:
    """Handle clients, game queues, running games and events"""

    def __init__(self, webserver):
        self.webserver = webserver # The webserver to send messages to clients
        self.players = {} # A map of clients where the key is the ID given by the webserver
        self.games = {} # A map of games where the 
        self.gameid = count(1)
        self.queues = {}
        self.running = True

    def connect(self, client: dict) -> str:
        """Register a client and give him an auth token"""

        if not self.running:
            raise ServerStopping("The server is currently stopping")

        if client["id"] in self.players:
            raise AlreadyRegistered("This client ID has already been registered")

        self.players[client["id"]] = {
            "socket": client, # The socket to send him messages
            "alive": True, # Weither the socket is alive or not
            "gameid": None, # The game the client is in
            "queue": None, # The queue the client is in
            "token": hexlify(urandom(int(TOKEN_LENGTH / 2))).decode() # An auth token
        }

        return self.players[client["id"]]["token"]


    def disconnect(self, client_id: int) -> None:
        """Try to disconnect a client
        The client isn't in game -> removed from manager
        The client is in game -> killed in game"""

        if client_id not in self.players:
            raise NotRegistered("This client hasn't registered yet")

        player = self.players[client_id]

        # If the client was in a queue, leave it
        if player["queue"] is not None:
            logger.info("Player ID %d left his queue", client_id)
            self.queues[player["queue"]].remove(client_id)

        # If the client was not in game or the game manager is stopping, remove him
        if player["gameid"] is None or not self.running:
            logger.info("Player ID %d removed from game manager", client_id)
            del self.players[client_id]

        # Otherwise kill him in that game (to keep the player mapping until the game stops)
        else:
            logger.info("Player ID %d has been killed in game ID %d", client_id, player["gameid"])
            self.games[player["gameid"]]["game"].kill(client_id)
            player["alive"] = False

    def join_queue(self, client_id, queue):
        """Join a game queue. If queue is full, empty it to a new game and start it"""

        if not self.running:
            raise ServerStopping("The server is currently stopping")

        if queue not in GAMES:
            raise InvalidQueue("Valids queue are: " + ", ".join(GAMES.keys()))

        if client_id not in self.players:
            raise NotRegistered("This client hasn't registered yet")

        player = self.players[client_id]

        if player["gameid"] is not None:
            raise InGame("The player must finish his game before joining the queue")

        # If the client was in another queue, leave it
        if player["queue"] is not None:
            self.queues[player["queue"]].remove(client_id)

        # Update the client mapping
        player["queue"] = queue

        # If the queue isn't in the mapping, create a new one
        if queue not in self.queues:
            self.queues[queue] = deque()

        # Make the client join the queue
        self.queues[queue].append(client_id)

        logger.info("Client ID %d joined queue %s [%d/%d]", client_id, queue, len(self.queues[queue]), PLAYERS_PER_GAME)

        # If the queue is full, start a new game
        if len(self.queues[queue]) >= PLAYERS_PER_GAME:
            self.start_game(queue)

    def start_game(self, queue):
        """Start a game from a queue"""

        # Get a new ID for this game
        gid = next(self.gameid)

        # Get players from the queue
        players = [self.queues[queue].popleft() for n in range(PLAYERS_PER_GAME)]

        logger.info("Starting game %s with ID %d and players %s", GAMES[queue]["gamefunc"].__name__, gid, str(players))

        # Init a new game and retrieve the startup message
        game = GAMES[queue]["gamefunc"](gid, players, **GAMES[queue]["initfunc"]())
        startup_status = json.dumps({"cmd": "startup_status", **game.get_startup_status()})
        logger.debug("Game ID %d sent the following command: %s", gid, startup_status)

        # Update the player mapping and send them the startup message
        for player in players:
            self.players[player]["queue"] = None
            self.players[player]["gameid"] = gid
            if self.players[player]["alive"]:
                self.webserver.send_message(self.players[player]["socket"], startup_status)

        # Update the game mapping with the game object and the thread
        self.games[gid] = {}
        self.games[gid]["game"] = game
        self.games[gid]["thread"] = Thread(
            target=self.game_handler,
            args=(gid, game, players),
            daemon=True
        )

        # Start the game
        self.games[gid]["thread"].start()

    def game_handler(self, gid, game, players):
        """Handle a game, calling the "main" method each tick, retrieve the game status to send it to the players"""
        try:
            tick = Event()

            # Main loop stop when the game is over or when the game manager is closing
            while self.running:
                # After the given time, the tick event is set and the loop.. loop :p
                Timer(1 / GAME_TICKS_PER_SECOND, tick.set).start()

                # Call the main method of the game and retrieve a status
                status = game.main()

                # If something happened during this tick, send the status to the players
                if status["didsmthhappen"]:
                    status_json = json.dumps({"command": "status", "status": status})
                    logger.debug("Game ID %d sent the following command: %s", gid, status_json)

                    for pid in players:
                        if self.players[pid]["alive"]:
                            self.webserver.send_message(self.players[pid]["socket"], status_json)

                # If the game is over, exit main loop
                if game.gameover:
                    if status["winner"] is not None:
                        logger.info("Game ID %d has been won by Player ID %d", gid, status["winner"])


                    break

                # Wait the tick to be over, clear it and loop
                tick.wait()
                tick.clear()

        except Exception as e:

            # If any exception occurs, stop the game without winner and log the exception
            logger.exception(repr(e))
            for pid in players:
                if self.players[pid]["alive"]:
                    self.webserver.send_message(self.players[pid]["socket"], json.dumps({"error": repr(e)}))

        finally:
            logger.info("Game ID %d is over", gid)

            # Update the players mapping
            for pid in players:
                self.players[pid]["gameid"] = None

                # If the player has been disconnected and hasn't reconnected, disconnect him for real
                if not self.players[pid]["alive"]:
                    self.disconnect(pid)

            # Delete the game
            del self.games[gid]

    def send_event(self, client_id, event, *args, **kwargs):
        """Send an event (call a function with the given args) in the game the player is in"""
        if client_id not in self.players:
            raise NotRegistered("This client hasn't registered yet")

        player = self.players[client_id]
        if player["gameid"] is None:
            raise NotInGame("The player must be in game to send an event")

        game = self.games[player["gameid"]]["game"]
        events = game.get_events()
        if event not in events:
            raise InvalidEvent("{} is not a valid event, valids one are: {}".format(event, ", ".join(events)))

        logger.debug("Player ID %d fired event \"%s\" to Game ID %d", client_id, event, self.players[client_id]["gameid"])
        game.run_event(client_id, event, *args, **kwargs)
        
    def safe_stop(self):
        """Disconnect all users and wait for all game the stop"""
        
        logger.info("Stopping Game Manager")

        self.running = False
        for gid in self.games.copy():
            if gid in self.games:
                try:
                    self.games[gid]["thread"].join()
                except Exception as ex:
                    logging.exception("An exception occured on closing Game ID %d", gid)

        for pid in self.players.copy():
            if pid in self.players:
                try:
                    self.disconnect(pid)
                except Exception as ex:
                    logging.exception("An exception occured on kicking Player ID %d", pid)

        logger.info("All game terminated and all clients kicked")