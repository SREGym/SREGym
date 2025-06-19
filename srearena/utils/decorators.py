# Set the fault_injected flag when inject_fault is called
def mark_fault_injected(method):
    def wrapper(self, *args, **kwargs):
        result = method(self, *args, **kwargs)
        self.fault_injected = method.__name__ == "inject_fault"
        return result

    return wrapper
