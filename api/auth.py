from django.core.cache import cache
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed, Throttled
from rest_framework.throttling import SimpleRateThrottle

MAX_FAILURES = 20  # wrong tokens per address per minute

from tracking.models import ApiToken
from tracking.owner import act_for_rest_of_request


class TokenUser:
    """Stands in for a user: the API is used by tokens, not by logins.
    Data is read and written as the token's owner."""

    is_authenticated = True
    is_anonymous = False

    def __init__(self, token: ApiToken):
        self.token = token
        self.pk = f"token-{token.pk}"


class BearerToken(BaseAuthentication):
    """Authorization: Bearer <token>, checked against the stored SHA-256."""

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != b"bearer":
            return None
        if len(parts) != 2:
            raise AuthenticationFailed("Send the header as: Authorization: Bearer <token>")
        try:
            token = ApiToken.lookup(parts[1].decode())
        except UnicodeDecodeError:
            token = None
        if token is None:
            self.count_failure(request)
            raise AuthenticationFailed("That token is not valid. Create a new one in Settings.")
        ApiToken.all_users.filter(pk=token.pk).update(last_used_at=timezone.now())
        if not token.user.is_active:
            raise AuthenticationFailed("That token's account is disabled.")
        act_for_rest_of_request(token.user)
        return TokenUser(token), token

    @staticmethod
    def count_failure(request):
        """DRF only throttles requests that authenticate, so guessing is
        limited here: too many wrong tokens from one address and it is
        locked out for a minute."""
        key = "api-bad-token-" + TokenThrottle().get_ident(request)
        cache.add(key, 0, timeout=60)
        if cache.incr(key) > MAX_FAILURES:
            raise Throttled(wait=60)

    def authenticate_header(self, request):
        return "Bearer"


class TokenThrottle(SimpleRateThrottle):
    scope = "token"

    def get_cache_key(self, request, view):
        if not getattr(request.user, "is_authenticated", False):
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}
