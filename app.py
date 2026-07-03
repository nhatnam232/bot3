#!/usr/bin/env python3
"""
Default starter file for bot servers.
Replace this file with your own bot code to get started.
"""

import time

def print_welcome():
    message = """
╔══════════════════════════════════════════════════════════════════════╗
║                                                                      ║
║   Your server is running and ready for setup!                        ║
║                                                                      ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║   GETTING STARTED                                                    ║
║   ───────────────                                                    ║
║                                                                      ║
║   1. Upload your bot files                                           ║
║      Go to the Files tab and upload your project files.              ║
║      Replace this app.py with your own code.                         ║
║                                                                      ║
║   2. Set your dependencies                                           ║
║      In the Startup tab, add your Python packages to the             ║
║      requirements field (e.g. discord.py, python-telegram-bot).      ║
║                                                                      ║
║   3. Configure your bot token                                        ║
║      Make sure to set your tokens in an .env-file.                   ║
║      Never hardcode tokens in your code!                             ║
║                                                                      ║
║   4. Restart your server                                             ║
║      Click Restart to apply your changes and launch your bot.        ║
║                                                                      ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║   TIPS                                                               ║
║   • Use the Console tab to monitor your bot's output                 ║
║   • Check our documentation at docs.fps.ms for tutorials             ║
║   • Need help? Open a support ticket from your dashboard             ║
║                                                                      ║
╠══════════════════════════════════════════════════════════════════════╣
║                                                                      ║
║   NOTE: The console is not a terminal. You can send input to your    ║
║   bot, but you cannot run system commands like "npm init".           ║
║                                                                      ║
╚══════════════════════════════════════════════════════════════════════╝
"""
    print(message)

if __name__ == "__main__":
    print_welcome()

    # Keep running so users can read the message
    print("\nThis placeholder will exit in 2 minutes.")
    print("Replace this file with your bot code and restart the server.\n")
    time.sleep(120)
