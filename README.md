# Minecraft Tier List Blacklist Bot

A Discord bot for a Minecraft tier-list server.

## Commands

### `/blacklist <user> <days>`
Only users whose highest role is **Moderator** or higher can use this.

It:
- Adds the existing `Blacklisted` role.
- Removes:
  - `Crystal-Waitlist`
  - `Mace-Waitlist`
  - `Sword-Waitlist`
  - `Axe-Waitlist`
  - `Spear Mace-Waitlist`
  - `UHC-Waitlist`
  - `SMP-Waitlist`
  - `Pot-Waitlist`
  - `NethPot-Waitlist`
- Stores the expiry time in SQLite.
- Sends:
  > You have been Blacklisted for X days. You will have to manually get the waitlist roles once your Blacklisted period is over
- Automatically removes the `Blacklisted` role when the period ends.

If a user is already blacklisted, running the command again resets their timer to the new duration.

### `/unblacklist <user>`
Moderator or higher only. Removes the user's `Blacklisted` role and deletes their blacklist timer. It does **not** restore any waitlist roles; they must manually get them again.

Moderators cannot blacklist users who have any of these protected roles: **Administrator, Admin, Owner, Wiado**. They also cannot blacklist themselves.

### `/blacklisted`
Everyone can use it. Lists currently blacklisted users and their remaining time.

### `/timeleft <user>`
Everyone can use it. Shows the remaining blacklist time for that user.

## Setup

1. Create a bot application in the Discord Developer Portal.
2. Enable the **Server Members Intent** under Bot -> Privileged Gateway Intents.
3. Invite the bot with:
   - `bot` scope
   - `applications.commands` scope
   - permissions: **Manage Roles** (and Send Messages/View Channel).
4. Put the bot's role **above `Blacklisted` and every waitlist role** in your server's role hierarchy.
5. Copy `.env.example` to `.env`.
6. Put your bot token and server ID in `.env`.
7. Install Python 3.10+.
8. Run:

   ```bash
   pip install -r requirements.txt
   python bot.py
   ```

The bot uses SQLite (`blacklist.db`) so blacklist timers survive restarts.

## Important role setup

The code looks for roles by their exact names. Make sure these names match:

- `Moderator`
- `Blacklisted`
- `Crystal-Waitlist`
- `Mace-Waitlist`
- `Sword-Waitlist`
- `Axe-Waitlist`
- `Spear Mace-Waitlist`
- `UHC-Waitlist`
- `SMP-Waitlist`
- `Pot-Waitlist`
- `NethPot-Waitlist`

If your moderator role has a different name, change `MODERATOR_ROLE_NAME` in `.env`.
