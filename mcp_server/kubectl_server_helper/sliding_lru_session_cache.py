import logging
import os
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path

from mcp_server.kubectl_server_helper.kubectl_tool_set import KubectlToolSet

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
mcp_data_dir = Path(__file__).parent.parent / "data"


class SlidingLRUSessionCache:
    """
    This is a container that used to hold session data of kubectl mcp tools.
    Features:
        Fixed max_size: evicts least recently used items
        Sliding TTL: expiration timer is refreshed on every access
        Thread-safe (via threading.Lock)
        Automatic cleanup on access and insertion
    """

    def __init__(self, max_size: int, ttl_seconds: int | float):
        self.max_size = max_size
        self.ttl = ttl_seconds
        self.lock = threading.RLock()
        self.cache = OrderedDict()  # key -> (value, last_access_time)

    def __getitem__(self, key) -> KubectlToolSet:
        with self.lock:
            self.clean_expired()
            if key not in self.cache:
                raise KeyError(key)

            value, last_access = self.cache[key]
            # Refresh TTL (sliding expiration)
            logger.info(f"Accessing item with key {key}. TTL is refreshed.")
            now = time.time()
            self.cache.move_to_end(key)
            self.cache[key] = (value, now)
            return value

    def __setitem__(self, key, value: KubectlToolSet):
        with self.lock:
            now = time.time()
            if key in self.cache:
                self.cache.move_to_end(key)
            self.cache[key] = (value, now)

            if self.__len__() > self.max_size:
                to_del = next(iter(self.cache))
                logger.info(f"Clean up LRU item with key {to_del} as maxsize is reached.")
                self.__delitem__(to_del)  # remove LRU

    def __delitem__(self, key):
        with self.lock:
            tool, last_access = self.cache[key]
            self._clean_up_tool(key, tool)
            del self.cache[key]

    # length of unexpired ones
    def __len__(self):
        with self.lock:
            self.clean_expired()
            return len(self.cache)

    def clean_expired(self):
        with self.lock:
            now = time.time()
            to_dels = []
            for key in self.cache:
                value, last_access = self.cache[key]
                if now - last_access >= self.ttl:
                    to_dels.append(key)
                else:
                    # all the items behind the first unexpired shouldn't be expired either.
                    break

            for to_del in to_dels:
                logger.info(f"Clean up expired items with key {to_del}.")
                self.__delitem__(to_del)

    def get(self, key, default=None):
        """
        Use this method to get tools. If the returned
        result is None, it means the key doesn't exist.
        """
        try:
            return self[key]
        except KeyError:
            return default

    def set(self, key, value):
        self[key] = value

    def size(self):
        return len(self)

    def _clean_up_tool(self, key, tool: KubectlToolSet):
        """
        Clean up the directory created for the tool
        related with the session {key}
        """
        opt_dir = Path(tool.config.output_dir)
        if os.path.exists(opt_dir) and os.path.isdir(opt_dir):
            if opt_dir.parent == mcp_data_dir:
                logger.info(f"Tool file directory {opt_dir} of session {key} will be deleted.")
                shutil.rmtree(opt_dir)
            else:
                logger.info(
                    f"Tool file directory {opt_dir} of session {key} is not the default one. "
                    f"For safety issues, please clean it up by yourself."
                )
        else:
            logger.info(
                f"Tool file directory {opt_dir} of session {key} does not exist when trying to clean it or "
                f"path {opt_dir} is not a valid directory."
            )
