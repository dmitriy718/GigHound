"""Session seeding utility: log a platform in once, by hand.

    python -m worker.login --platform upwork [--user-id 1]

Opens a HEADED browser on the platform's login page using the same
persistent profile the worker will use. Log in manually (including any
2FA/CAPTCHA — this is the sanctioned human moment), then press Enter here;
the authenticated session is saved under WORKER_SESSION_DIR and reused by
the worker from then on.
"""
import argparse
import logging

from .browser import BrowserManager
from .config import Config
from .platforms import platform_config

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed a platform browser session")
    parser.add_argument("--platform", required=True,
                        help="platform key from worker/platforms.py (e.g. upwork)")
    parser.add_argument("--user-id", type=int, default=1,
                        help="backend user id this session belongs to")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    cfg = platform_config(args.platform)
    config = Config(headless=False)  # headed, always
    config.session_dir.mkdir(parents=True, exist_ok=True)

    with BrowserManager(config) as browser:
        page = browser.new_page(args.platform, args.user_id)
        page.goto(cfg["login_url"], wait_until="domcontentloaded")
        print(f"\nLog in to {args.platform} in the browser window "
              f"(complete any 2FA/CAPTCHA), then press Enter here... ")
        input()
        browser.close_session(args.platform, args.user_id)
    print(f"Session saved under {browser.session_dir_for(args.platform, args.user_id)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
