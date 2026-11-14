from console import create_app

app = create_app()

if __name__ == "__main__":
    import os

    app.run(
        host=os.getenv("MSP_TEST_HOST", "0.0.0.0"),
        port=int(os.getenv("MSP_TEST_PORT", "18080")),
        debug=os.getenv("MSP_TEST_DEBUG", "false").lower() in {"1", "true", "yes"},
        threaded=True,
    )
