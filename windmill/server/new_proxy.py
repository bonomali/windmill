import socket
import httplib
import logging
import urllib
from copy import copy
from urlparse import urlparse

from webenv import Application, Response, Request, Response302, HtmlResponse
import httplib2

logger = logging.getLogger()

global_exclude = ['http://sb-ssl.google.com',
                  'https://sb-ssl.google.com', 
                  'http://en-us.fxfeeds.mozilla.com',
                  'fxfeeds.mozilla.com',
                  'http://www.google-analytics.com',
                  ]


# Note that hoppish conntains proxy-connection, which is pre-HTTP-1.1 and
# is somewhat nebulous
hoppish_headers = {'connection':1, 'keep-alive':1, 'proxy-authenticate':1,
                   'proxy-authorization':1, 'te':1, 'trailers':1, 'transfer-encoding':1,
                   'upgrade':1, 'proxy-connection':1, 
                   'p3p':1 #Not actually a hop-by-hop header, just really annoying 
                   }

# Cache stopping headers
cache_headers = {'Pragma':'no-cache', 'Cache-Control': 'post-check=0, pre-check=0',
                 'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
                 'Expires': '-1'}

cache_removal = [k.lower() for k in cache_headers.keys()]
cache_additions = cache_headers.items()

class ProxyResponse(Response):
    def __init__(self, resp):
        self.http2lib_response = resp
        self.status = resp['status']
        self.httplib_response = resp._response
        # Anything under .5 meg just return
        # Anything over .5 meg return in 100 chunked reads
        if self.httplib_response.length > 512000:
            self.read_size = self.httplib_response.length / 100
        else:
            self.read_size = self.httplib_response.length
        self.content_type = resp['content-type']
    
    def __iter__(self):
        yield self.httplib_response.read(self.read_size)
        while self.httplib_response.chunk_left is not None:
            if self.httplib_response.chunk_left < self.read_size:
                yield self.httplib_response.read()
                self.httplib_response.chunk_left = None
            else:
                yield self.httplib_response.read(self.read_size)
        
class WindmillHttp(httplib2.Http):
    def _conn_request(self, conn, request_uri, method, body, headers):
        """Customized response code for Windmill."""
        for i in range(2):
            try:
                conn.request(method, request_uri, body, headers)
            except socket.gaierror:
                conn.close()
                raise httplib2.ServerNotFoundError("Unable to find the server at %s" % conn.host)
            except (socket.error, httplib.HTTPException):
                # Just because the server closed the connection doesn't apparently mean
                # that the server didn't send a response.
                pass
            try:
                response = conn.getresponse()
            except (socket.error, httplib.HTTPException):
                if i == 0:
                    conn.close()
                    conn.connect()
                    continue
                else:
                    raise
            else:
                # Decompression was removed from this section as was HEAD request checks.
                resp = httplib2.Response(response)
                resp._response = response
            break
        # Since content is never checked in the rest of the httplib code we can safely return
        # our own windmill response class here.
        proxy_response = ProxyResponse(resp)
        return (resp, proxy_response)

class ProxyClient(object):
    def __init__(self, fm):
        self.http = WindmillHttp()
        self.fm = fm

    def is_hop_by_hop(self, header):
      """check if the given header is hop_by_hop"""
      return hoppish_headers.has_key(header.lower())
    
    def clean_request_headers(self, request, host):
        headers = {}
        for key, value in request.headers.items():
            if '/windmill-serv' in value:
                value = value.split('/windmill-serv')[-1]
            if not self.is_hop_by_hop(key):
                headers[key] = value
        if 'host' not in headers:
            headers['host'] = request.environ['SERVER_NAME']   
    
    def set_response_headers(self, resp, response, request_host, proxy_host):
        # TODO: Cookie handler on headers
        response.headers = [(k,v.replace(proxy_host, request_host),) for k,v in resp.items()]
    
    def make_request(self, request, host):
        uri = request.full_uri.replace(request.host, host, 1)
        headers = self.clean_headers(request, host)
        resp, response = self.http.request(uri, method=request.method, body=str(request.body),
                                           headers=headers, redirections=0)
        self.set_response_headers(resp, response, request.host, host)
        return response
    

class ForwardMap(dict):
    def __init__(self, *args, **kwargs):
        dict.__init__(self, *args, **kwargs)
        self.ordered_hosts = []
    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        self.ordered_hosts.append(value)
    def known_hosts(self, first_forward_hosts, exclude_hosts):
        hosts = first_forward_hosts
        for host in self.ordered_hosts:
            if host not in hosts and host not in exclude_hosts:
                hosts.append(host)
        return hosts
        
class ForwardingManager(object):
    mapped_response_pass_codes = [200]
    mapped_response_pass_threshold = 399
    unmapped_response_pass_codes = [200]
    unmapped_response_pass_threshold = 399
    first_forward_hosts = []
    exclude_from_retry = copy(global_exclude)
    
    def __init__(self, forwarding_test_url=None):
        self.environ_conditions = []
        self.request_conditions = []
        self.response_conditions = []
        self.initial_forward_map = {}
        self.forward_map = ForwardMap()
        self.redirect_forms = {}
        
    def set_test_url(self, test_url):
        if test_url is None:
            self.enabled = False
            self.forwarding_test_url = None
            self.test_url = None
            self.test_host = None
        else:
            self.enabled = True
            self.forwarding_test_url = test_url
            self.test_url = urlparse(test_url)
            self.test_host = self.test_url.scheme+"://"+self.test_url.netflow
    
    def is_mapped(self, request):
        if (request.full_uri in self.forward_map):
            return True
        if (request.environ.get('HTTP_REFERER', None) in self.forward_map):
            return True
        return False
    
    def get_forward_host(self, request):
        """Check if a request uri is in the forward map by uri and referer"""
        if request.full_uri in self.forward_map:
            return self.forward_map[request.full_uri]
        # Check referer, use tripple false tuple in case someone added a constant to 
        # the forward map
        if request.environ.get('HTTP_REFERER', (False, False, False)) in self.forward_map:
            return self.forward_map[request.environ['HTTP_REFERER']]
        return None
    
    def create_redirect_form(self, request, uri):
        inputs = ['<input type="hidden" name="%s" value="%s" />' % 
                  (urllib.unquote(k), urllib.unquote(v),) for k, v in request.body.form.items()
                  ]            
        form = """<html><head><title>There is no spoon.</title></head>
    <body onload="document.getElementById('redirect').submit();"
          style="text-align: center;">
      <form id="redirect" action="%s" method="POST">%s</form>
    </body></html>""" % (uri, '\n'.join(inputs))
        self.redirect_forms[uri] = form.encode('utf-8')
    
    def is_form_forward(self, request):
        if self.redirect_forms.has_key(request.uri):
            return True
        return False
        
    def form_forward(self, request):
        form = self.redirect_forms.pop(request.uri)
        return form
    
    def initial_forward(self, request):
        new_uri = request.full_uri.replace(request.host, self.test_host)
        self.forward_map[new_uri] = request.host
        return new_uri
    
    def forwardable(self, request):
        if request.url.netloc.startswith('127.0.0.1') or (
           not self.proxy_conditions_pass(request)):
            return False
        return True
    
    add_environ_condition = lambda self, condition: self.environ_conditions.append(condition)
    add_request_condition = lambda self, condition: self.request_conditions.append(condition)
    add_response_condition = lambda self, condition: self.response_conditions.append(condition)
         
    def proxy_conditions_pass(self, request):
        for condition in self.environ_conditions:
            if not condition(request.environ):
                return False
        for condition in self.request_conditions:
            if not condition(request):
                return False
        return True
        
    def response_conditions_pass(self, request, target_host, client_response, mapped):
        for condition in self.response_conditions:
            result = condition(request, target_host, client_response, mapped)
            if result is not None:
                return result
        if mapped: g = 'mapped_'
        else: g = 'unmapped_' 
        if client_response.status in getattr(self, g+'_response_pass_codes'):
            return True
        if client_response > getattr(self, g+'_response_pass_threshold'):
            return False
        else:
            return True
            
    def get_retry_hosts(self, request):
        return self.forward_map.known_hosts(self.first_forward_hosts, self.exclude_from_retry)

class Response(object):
    """WSGI Response Abstraction. Requires that the request object is set to it before being returned in a wsgi application."""

    content_type = 'text/plain'
    status = '200 OK'

    def __init__(self, body=''):
        self.body = body
        self.headers = []

    def __iter__(self):
        self.headers.append(('content-type', self.content_type,))
        self.request.start_response(self.status, self.headers)
        if not hasattr(self.body, "__iter__"):
            yield self.body
        else:
            for x in self.body:
                yield x

class InitialForwardResponse(Response302):
    def __init__(self, request):
        super(InitialForwardResponse, self).__init__(request.uri)
        self.headers = cache_headers

# class ProxyResponse(Response):
#     def __init__(self, forwarding_manager, request):
#         Response.__init__(self)
#         self.fm = forwarding_manager
#         self.request = request
    
class ProxyApplication(Application):
    def __init__(self):
        super(ProxyApplication, self).__init__()
        self.fm = ForwardingManager()
        self.client = ProxyClient(self.fm)
    
    def handler(self, request):
        if self.fm.enabled() and self.fm.forwardable(request):
            hosts = self.fm.get_hosts(request)
            if request.host != self.fm.test_host:
                # request host is not the same as the test host, we need to do an initial forward
                new_uri = self.fm.initial_forward(request)
                if hasattr(request.body, 'form'): # form objects are only created for http forms
                    self.fm.create_redirect_form(request, new_uri)
                logger.debug('Domain change, forwarded to ' + new_uri)
                return InitialForwardResponse(new_uri)
            elif self.fm.is_form_forward(request):
                form = self.fm.form_forward(request)
                response = HtmlResponse(form)
                response.headers += cache_additions
                return response
            
            # At this point we are 100% sure we will be needing to send a proxy request
            
            # If the host has been mapped by uri or referrer go with that
            target_host = self.fm.get_forward_host(request)
            if target_host is not None:
                targeted_client_response = self.client.make_client_request(request, target_host)
                if self.fm.response_conditions_pass(request, target_host, 
                                                    targeted_client_response, mapped=True):
                    return targeted_client_response
            else:
                targeted_client_response = None
            
            # Now we've hit the retry loop
            for host in self.fm.get_retry_hosts(request):
                client_response = self.client.make_client_request(request, host)
                if self.fm.response_conditions_pass(request, host, client_response, mapped=False):
                    return response
                
            # At this point all requests have failed
            if targeted_client_response:
                # If we had a mapped response return it even if it failed
                return targeted_client_response
            else:
                # If we don't even have a mapped response, return it form test host
                return self.client.make_client_request(request, request.host)

# 
# class IterativeResponse(object):
