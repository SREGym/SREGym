def mark_fault_injected(method=None, *, strict=False):
    """Track fault state; optionally propagate recovery errors as well."""
    if method is None:
        return lambda wrapped: mark_fault_injected(wrapped, strict=strict)

    def wrapper(self, *args, **kwargs):
        try:
            result = method(self, *args, **kwargs)
        except Exception as e:
            if method.__name__ == "inject_fault" or strict:
                # Injection and opt-in strict recovery must preserve failures.
                raise
            else:
                print(f"[{method.__name__}] Warning: encountered error: {e!r}")
                result = None

        self.fault_injected = method.__name__ == "inject_fault"
        return result

    return wrapper
