"""python -m seance  →  lights the candles and opens the graveyard in your browser."""
import os
import threading
import webbrowser

import uvicorn

BANNER = r"""
        .-----.
      .'  RIP  '.        git séance
      |  your   |        talk to the projects you left behind
      |  side   |
      | project |        graveyard : {url}
   ___|_________|___     spirit    : {model} via Ollama (local)
"""


def main() -> None:
    from . import spirit

    host = os.getenv("SEANCE_HOST", "127.0.0.1")
    port = int(os.getenv("SEANCE_PORT", "1031"))  # October 31
    url = f"http://{host}:{port}"
    print(BANNER.format(url=url, model=spirit.MODEL))
    if os.getenv("SEANCE_NO_BROWSER") != "1":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("seance.server:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
