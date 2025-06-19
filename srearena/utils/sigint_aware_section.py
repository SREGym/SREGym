import signal


class SigintAwareSection:
    def __enter__(self):
        # Save the original signal handler
        self.original_handler = signal.signal(signal.SIGINT, self.signal_handler)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Restore the original signal handler
        signal.signal(signal.SIGINT, self.original_handler)
        return False  # Do not suppress exceptions

    def signal_handler(self, signum, frame):
        print("\nCtrl+C detected!")
        raise KeyboardInterrupt
