import logging

local_logger = logging.getLogger('all.sregym')
local_logger.propagate = True
local_logger.setLevel(logging.DEBUG)