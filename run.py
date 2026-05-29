from dotenv import load_dotenv
load_dotenv()

import uvicorn
from scheduler.config.settings import get_settings


def main():
    settings = get_settings()
    uvicorn.run(
        "scheduler.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
