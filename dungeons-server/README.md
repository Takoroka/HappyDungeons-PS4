# Happy Dungeons server (the friends server)

The server behind the Happy Dungeons revival: a single-file Python/Flask program (`experimentalserver.py`) that
stands in for the Xbox Live services the PC (WinDurango) build of Happy Dungeons expects. It holds player
identities, saves, friends, lobbies and matchmaking, events and the shop data, achievements, and the players
website. The PS4 build reaches the same players and saves through the PS4 gateway in `../server/hd_ps4_gateway.py`,
which runs next to it.

This is the code as it runs on the live server (November 2026). See `SERVER-README.txt` for what each part does,
the ports and the settings.

## What is here

| Path | What |
|---|---|
| `experimentalserver.py` | The server. |
| `requirements.txt` | Python packages (Flask, websockets). |
| `server_config.example.json` | Every setting, documented. Copy to `server_config.json` and edit. |
| `web/index.html` | The players website (`/windurango/`): who is online, friends, lobbies, events and updates for owners, save export, achievements, and the PS4 (shadPS4) login box. |
| `deploy/` | Running it on an Ubuntu VPS: `README-deploy.txt`, `setup.sh` (on the VPS), `deploy.ps1` (from Windows). |
| `eventorder/` | Event-order rotation for the shop and daily orders (`roll_event_orders_v2.py`, `hdpak.py`). |

## Running it locally

```
pip install -r requirements.txt
cp server_config.example.json server_config.json
python3 experimentalserver.py
```

It listens on 127.0.0.1 by default: HTTP 5000, RTA websocket 8765, UDP relay 3075.

## Not included

- Player data: the `data/` folder, with identities and their secrets, saves, friends and achievements.
- The live `server_config.json`, which holds the owners list and the client-update signing key.
- Logs and backups.
- Game files. `eventorder/` needs an `event_order.template.pak` taken from your own copy of the game.
