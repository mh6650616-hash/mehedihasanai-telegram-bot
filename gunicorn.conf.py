# Optional: if you prefer to start background thread using Gunicorn post_fork
def post_fork(server, worker):
    # import inside function to avoid circular import during master process load
    try:
        from main import _start_background_thread_once
        _start_background_thread_once()
    except Exception as e:
        server.log.warning("Failed to start background thread in post_fork: %s", e)
