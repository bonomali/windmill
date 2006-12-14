#   Copyright (c) 2006 Open Source Applications Foundation
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import windmill.server
import logging, time
from threading import Thread

def setup_server(console_level=logging.INFO):
    """Setup the server and return httpd and loggers"""
    console_handler = windmill.server.logger.setup_root_logger(console_level=console_level)
    
    # Set loggers for each necessary area
    
    loggers = {'server':{'proxy':   windmill.server.logger.setup_individual_logger('server.proxy'),
                         'serv':    windmill.server.logger.setup_individual_logger('server.serv'),
                         'xmlrpc':  windmill.server.logger.setup_individual_logger('server.xmlrpc'),
                         'jsonrpc': windmill.server.logger.setup_individual_logger('server.jsonrpc'),
                         'wsgi':    windmill.server.logger.setup_individual_logger('server.wsgi')},
               'browser': windmill.server.logger.setup_individual_logger('browser')}
               
    httpd = windmill.server.wsgi.make_windmill_server(server_loggers=loggers['server'])
    return httpd, loggers, console_handler

def run_threaded(console_level=logging.INFO):
    """Run the server with various values"""

    httpd, loggers, console_handler = setup_server(console_level)

    httpd_thread = Thread(target=httpd.serve_until)
    httpd_thread.start()
    time.sleep(1)
    return httpd, httpd_thread, loggers, console_handler
    

    
if __name__ == "__main__":
    
    main()