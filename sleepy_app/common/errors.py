GEOIP_VISITOR_RETRY_SECONDS = 10
WEATHER_VISITOR_RETRY_SECONDS = 10

class GeoIpClientError(ValueError):
    pass

class GeoIpUpstreamError(RuntimeError):
    pass

class GeoIpRateLimitExceeded(RuntimeError):
    def __init__(self, message, retry_after=GEOIP_VISITOR_RETRY_SECONDS):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))

class WeatherConfigError(RuntimeError):
    pass

class WeatherUpstreamError(RuntimeError):
    pass

class WeatherRateLimitExceeded(RuntimeError):
    def __init__(self, message, retry_after=WEATHER_VISITOR_RETRY_SECONDS):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))
