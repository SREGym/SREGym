import logging


class ExhaustInfoFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None, style="%", extra_attributes=None):
        if extra_attributes is None:
            extra_attributes = []
        super().__init__(fmt, datefmt, style)
        self.base_format_string = fmt
        self.extra_attributes = extra_attributes

    def format(self, record):
        # 1. Execute parent class format() method to get base formatted log string
        # This step handles standard fields like %(asctime)s, %(levelname)s, %(message)s
        base_log_message = super().format(record)

        # 2. Find all non-standard (i.e., extra) fields
        extra_fields = {key: value for key, value in record.__dict__.items() if key in self.extra_attributes}

        # 3. Build extra fields append string
        extra_output = ""
        if extra_fields:
            # Format extra fields as 'key1=value1, key2=value2, ...' form
            extra_parts = [f"{key} = {value}" for key, value in extra_fields.items()]
            extra_output = " [" + ", ".join(extra_parts) + "]"

        # 4. Return base message and appended extra string
        return base_log_message + extra_output


class ColorFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None, style="%"):
        super().__init__(fmt, datefmt, style)

    def format(self, record):
        base_log_message = super().format(record)
        if record.levelno == logging.DEBUG:  # white
            return base_log_message
        elif record.levelno == logging.INFO:  # blue
            return f"\033[94m{base_log_message}\033[0m"
        elif record.levelno == logging.WARNING:  # yellow
            return f"\033[92m{base_log_message}\033[0m"
        elif record.levelno == logging.ERROR:  # red
            return f"\033[95m{base_log_message}\033[0m"
        return base_log_message
