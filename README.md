# PUBobot2
**PUBobot2** is a Discord bot for pickup games organisation. PUBobot2 have a remarkable list of features such as rating matches, rank roles, drafts, map votepolls and more!

### Some screenshots
![screenshots](https://cdn.discordapp.com/attachments/824935426228748298/836978698321395712/screenshots.png)

### Using the public bot instance
If you want to test the bot, feel free to join [**Pubobot2-dev** discord server](https://discord.gg/rjNt9nC).  
All the bot settings can be found and configured on the [Web interface](https://pubobot.leshaka.xyz/).  
For the complete list of commands see [COMMANDS.md](https://github.com/Leshaka/PUBobot2/blob/main/COMMANDS.md).  
You can invite the bot to your discord server from the [web interface](https://pubobot.leshaka.xyz/) or use the direct [invite link](https://discord.com/oauth2/authorize?client_id=177021948935667713&scope=bot).

### Support
Hosting the service for everyone is not free, not mentioning the actual time and effort to develop the project. If you enjoy the bot please subscribe on [Boosty](https://boosty.to/leshaka).

## Hosting the bot yourself

### Requirements
* **Python 3.9+** 
* **MySQL**.
* **gettext** for multilanguage support.

### Installing
* Create mysql user and database for PUBobot2:
* * `sudo mysql`
* * `CREATE USER 'pubobot'@'localhost' IDENTIFIED BY 'your-password';`
* * `CREATE DATABASE pubodb CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;`
* * `GRANT ALL PRIVILEGES ON pubodb.* TO 'pubobot'@'localhost';`
* Install required modules and configure PUBobot2:
* * `git clone https://github.com/Leshaka/PUBobot2`
* * `cd PUBobot2`
* * `pip3 install -r requirements.txt`
* * `cp config.example.cfg config.cfg`
* * `nano config.cfg` - Fill config file with your discord bot instance credentials and mysql settings and save.
* * Optionally, if you want to use other languages, run script to compile translations: `./compile_locales.sh`.
* * `python3 PUBobot2.py` - If everything is installed correctly the bot should launch without any errors and give you CLI.

## Credits
Developer: **Leshaka**. Contact: leshkajm@ya.ru.  
Used libraries: [discord.py](https://github.com/Rapptz/discord.py), [aiomysql](https://github.com/aio-libs/aiomysql), [emoji](https://github.com/carpedm20/emoji/), [glicko2](https://github.com/deepy/glicko2), [TrueSkill](https://trueskill.org/), [prettytable](https://github.com/jazzband/prettytable).

## License
Copyright (C) 2020 **Leshaka**.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License version 3 as published by the Free Software Foundation.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.

See 'GNU GPLv3.txt' for GNU General Public License.

### Modification notice
This work is a modified version of [Leshaka's PUBobot2](https://github.com/Leshaka/PUBobot2) (Copyright (C) 2020 **Leshaka**), distributed under the GNU General Public License version 3.

This project (PUBobot2) has been modified to add Diabotical Wipeout auto-reporting: a `bot/diabotical/` package that links Discord accounts to Diabotical user ids, a best-of-3 series matcher, and a background poller that files results through the existing report path. Original copyright retained; modifications are (C) 2026.

## Diabotical Wipeout auto-reporting

This is a self-hosted replacement for the public PUBobot in GetCracked! Tournaments `#pug`. One PUBobot *match* is a **best-of-3 series** of Diabotical Wipeout team games; each Diabotical `match_id` returned by the API is a single game, so the module reconstructs the series from the games and files the series score through the normal report path (ratings are unchanged — the existing `report_scores`/`register_match_ranked` still run).

### Account linking

The matcher keys off **Diabotical user ids**, never nicknames. Each operator links their Discord account to their Diabotical account once, then joins the pickup normally:

* `/link <user_id>` — validates the id against the Diabotical `users/{id}` endpoint, then stores the mapping (guild + Discord snowflake → Diabotical user id).
* `/unlink` — removes the mapping.
* `/link_status` — shows your linked id.

Only members drafted into a match are considered, so an unlinked player on a team simply does not participate in matching.

### The poller and the confirm flow

When a ranked match reaches the *waiting report* stage, a background poller starts (and is stopped when the match finishes, is cancelled, or times out). It watches Diabotical for the finished Wipeout games shared by the drafted players, requires **at least 6 of 8** linked player ids with consistent team assignment, and picks the single candidate that represents the series.

The behaviour is controlled by a single config flag:

```ini
# In config.cfg
DIABOTICAL_AUTO_REPORT = False   # default: off
```

* **`DIABOTICAL_AUTO_REPORT = True`** — once the series clinches (first to 2 games), the aggregated series score `[team0_wins, team1_wins]` is filed automatically through the normal report path.
* **`DIABOTICAL_AUTO_REPORT = False`** (default) — the bot posts a per-game scoreboard as each game finishes, then a series summary with the match id, map, frags and damage when the series clinches, and **waits for the operator** to confirm. The operator confirms with `/diabotical confirm`, which files the exact series score the poller already posted.

Every rejection (wrong mode, not finished, too few shared ids, disagreeing teams, etc.) is logged so a failed match is visible in the bot's output. Reporting is idempotent on the Diabotical `match_id` — a match already reported will not be re-reported.

### Replacing the public bot in `#pug`

Deploy this instance in the GetCracked! Tournaments guild and add the `/link`, `/unlink`, `/link_status` and `/diabotical` commands to the `#pug` channel (the bot must have access to the channel to post scoreboards). Both the auto-report mode and the manual `/diabotical confirm` fallback are drop-in replacements for the previous `/report` workflow, so no operator-side change is required beyond adding the new commands.
