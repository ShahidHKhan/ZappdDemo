"""Text with Bolt Burger in your terminal - same code path as the /sms webhook.

    python -m zappd.chat
"""

import sys

from zappd.agent_tools import POS
from zappd.conversation import OrderingService
from zappd.llm import LLMUnavailableError, get_client
from zappd.parser import parse_order


def main() -> None:
    try:
        get_client()  # fail fast with a clear message if there's no API key
    except LLMUnavailableError as e:
        sys.exit(str(e))
    service = OrderingService(parser=parse_order, pos=POS)
    phone = "+15555550100"
    print("Text Bolt Burger (Ctrl+C to quit). Try: 2 number 3s no pickles, one w/ a coke, and a large fry\n")
    while True:
        text = input("you > ").strip()
        if text:
            print("bolt >", service.handle_message(phone, text).replace("\n", "\n       "), "\n")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print()
