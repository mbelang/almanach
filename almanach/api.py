# Copyright 2016 Internap.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

from flask import Flask
from gunicorn.app.base import Application

from almanach import config
from almanach.adapters import api_route_v1 as api_route
from almanach import log_bootstrap
from almanach.adapters.database_adapter import DatabaseAdapter
from almanach.core.controller import Controller


class AlmanachApi(Application):
    def __init__(self):
        super(AlmanachApi, self).__init__()

    def init(self, parser, opts, args):
        log_bootstrap.configure()
        config.read(args)
        self._controller = Controller(DatabaseAdapter())

    def load(self):
        logging.info("starting flask worker")
        api_route.controller = self._controller

        app = Flask("almanach")
        app.register_blueprint(api_route.api)

        return app


def run():
    almanach_api = AlmanachApi()
    almanach_api.run()


if __name__ == "__main__":
    run()
