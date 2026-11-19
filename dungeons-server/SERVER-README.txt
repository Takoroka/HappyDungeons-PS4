A single-file Python/Flask server that stands in for the Xbox Live services
Happy Dungeons expects, so the game can run under WinDurango without the real
Xbox Live. It provides:

  * Identity and sign-in      players get an xuidBase and four XUIDs
  * MPSD sessions             the multiplayer session documents the game reads
  * SmartMatch matchmaking    hosts and seekers matched into lobbies
  * RTA websocket             the live connection the game holds open
  * UDP peer relay            carries dungeon traffic between players
  * Title storage / saves     save data, plus a save export as a ZIP
  * A client update channel   (signature-verified; off unless you configure it)

It listens on three ports: HTTP (default 5000), the RTA websocket (8765), and
the UDP relay (3075).


REQUIREMENTS
----------------------------------------------------------------------------
Python 3.9 or newer, and two packages:

    pip install flask websockets

(It was last run on Python 3.14. The `websockets` import uses the legacy
server API; if a future release drops it, pin `websockets<14`.)


HOW TO RUN IT LOCALLY
----------------------------------------------------------------------------
1. Put experimentalserver.py in a folder of its own. It keeps everything it
   creates next to itself, in a `data` subfolder it makes on first start.

2. Copy the example config next to it and edit it:

       copy server_config.example.json server_config.json

   It runs without this file at all, on safe defaults. The example is already
   set to those safe defaults, so you can also just copy it and start.

3. Start it:

       python experimentalserver.py

   You should see a banner ending with:

       HTTP compatibility server: http://127.0.0.1:5000

   and lines telling you there is no release published, no signing key, and
   no owners. That is the expected, safe state.

4. Point WinDurango at it. In winDurango.json set the Xbox Live server to
   http://127.0.0.1:5000 (use the machine's LAN IP instead if the game runs
   on a different box - and then set "bind" to "0.0.0.0", see below).

5. Check it is alive:

       curl http://127.0.0.1:5000/windurango/players

   200 with a JSON body means it is up. /windurango/me returning 401 is
   correct - that route needs a signed-in player.

Stop it with Ctrl+C.


VALUES YOU MUST SUPPLY
----------------------------------------------------------------------------
Everything privileged lives in configuration, never in the source. There are
three places, and all three are yours to create - none is shipped here.

(1) server_config.json  - next to experimentalserver.py
    Start from server_config.example.json, which documents all 18 keys inline
    via "_comment_*" entries that the server ignores. The ones that matter:

    owners            THE PRIVILEGED ALLOW-LIST. A list of xuidBase strings.
                      Owners, and only owners, may switch events, grant
                      jewels, revoke achievements, publish a client release,
                      set a minimum client version, and see every player on
                      the admin pages. Shipped EMPTY, which means nobody is
                      an owner - the safe state, and the reason every admin
                      route answers 401 on a fresh copy.
                      To make yourself an owner on your own server: start it,
                      let your game sign in once, then copy your "xuidBase"
                      out of data/_players.json into this list as a string.
                      Never copy anyone else's owner list.

    clientUpdateKey   The PUBLIC half of an RSA key used only to VERIFY the
                      signature on a client release you publish. The server
                      never signs anything, and it actively refuses a private
                      key here. Shipped EMPTY = no client updates served or
                      published, which is fine for client testing. Only needed
                      if you want to use the launcher update channel; generate
                      your own keypair and keep the private half offline.

    minClientVersion  Locks out launchers older than this. Shipped null =
                      nobody is blocked.

    partyIdSalt       Any random text. It is NOT a credential: it authenticates
                      nothing and gates no route. It is hashed with a session
                      name to make the opaque Discord party id, so that one
                      server's party ids do not collide with another's. Left
                      empty (as shipped) the server makes a random one at
                      start-up, prints a line saying so, and party ids change
                      when it restarts - perfectly fine for testing. To pin
                      them, put any string here, e.g. the output of
                      python -c "import secrets; print(secrets.token_hex(16))"

    bind              "127.0.0.1" (shipped) = this machine only. Use "0.0.0.0"
                      to let other machines reach it. On a public host keep it
                      on loopback and put a reverse proxy in front for HTTPS.

    The rest are ports and behaviour switches (relay, matchmaking policy,
    logging verbosity, peer-address handling) - all documented in the example.

(2) discord_app_id.txt  - next to experimentalserver.py, optional
    One line: the numeric Application ID of a Discord application YOU create
    at https://discord.com/developers/applications. Used for Rich Presence.
    Missing file = the Discord route answers null. Nothing breaks.

(3) data/  - created and maintained by the server itself
    You do not fill anything in here by hand. Note one file:

    data/_players.json  holds one random per-player login secret, generated
                        at first sign-in with secrets.token_hex(16). This is
                        effectively every player's password. Never share it,
                        never copy it between servers, never commit it.

    Other files the server creates as it goes: friends.json, achievements.json,
    jewel_balances.json, client_versions.json, save_export_log.jsonl, plus a
    folder of saves per player.


ONE DELIBERATE DIFFERENCE FROM THE AUTHOR'S COPY
----------------------------------------------------------------------------
The party id salt used to be a fixed string in the source; it is now read from
server_config.json "partyIdSalt" as described above. It is not a credential,
and this changes nothing you can observe except which opaque party id a
session gets. Everything else in this file is unchanged apart from four
comments and docstrings that mentioned the original author's own host layout
or example player names.


SECURITY NOTES FOR YOUR OWN DEPLOYMENT
----------------------------------------------------------------------------
* The built-in Flask server is a development server. If you ever expose this
  beyond your own machine, put a real WSGI server and a reverse proxy in
  front of it, and serve it over HTTPS.
* The UDP relay port is not proxied and needs to be reachable directly.
* "allowRealPeerAddresses" lets players publish their real IP to each other.
  Leave it false unless you are on a trusted LAN or VPN.
* If you publish client updates, keep the release-signing private key off the
  server. The server only ever needs the public half.
