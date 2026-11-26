"""
Run this ONCE on your own computer to log in and get your SESSION string.

  pip install telethon
  python login.py

It asks for your API ID, API hash, phone number and the code Telegram sends you
(and your 2FA password if you have one). Then it prints a long SESSION string.
Paste that into Render -> Environment -> SESSION.

The SESSION string = full access to your Telegram account. Never share it or post it anywhere.
"""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("API ID: ").strip())
api_hash = input("API hash: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    me = client.get_me()
    print(f"\nLogged in as {me.first_name}.")
    print("\nYour SESSION string (copy ALL of it, keep it secret):\n")
    print(client.session.save())
