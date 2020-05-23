import datetime

from django.core import signing
from django.test import TestCase, override_settings
from django.utils import timezone

from .packers import BasePacker
from .test_mixins import CaptureLogMixin, CreateUserMixin
from .test_signals import reset_sesame_settings  # noqa
from .tokens import create_token, packer, parse_token


class TestTokensBase(CaptureLogMixin, CreateUserMixin, TestCase):
    def test_valid_token(self):
        token = create_token(self.user)
        user = parse_token(token, self.get_user)
        self.assertEqual(user, self.user)
        self.assertLogsContain("Valid token for user %s" % self.username)


class TestTokens(TestTokensBase):
    def test_bad_token(self):
        token = create_token(self.user)
        user = parse_token(token.lower(), self.get_user)
        self.assertEqual(user, None)
        self.assertLogsContain("Bad token")

    def test_random_token(self):
        user = parse_token("!@#", self.get_user)
        self.assertEqual(user, None)
        self.assertLogsContain("Bad token")

    def test_unknown_user(self):
        token = create_token(self.user)
        self.user.delete()
        user = parse_token(token, self.get_user)
        self.assertEqual(user, None)
        self.assertLogsContain("Unknown or inactive user")

    def test_token_invalidation_when_password_changes(self):
        token = create_token(self.user)
        self.user.set_password("hunter2")
        self.user.save()
        user = parse_token(token, self.get_user)
        self.assertEqual(user, None)
        self.assertLogsContain("Invalid token")

    @override_settings(SESAME_MAX_AGE=300)
    def test_valid_max_age_token(self):
        self.test_valid_token()

    @override_settings(SESAME_MAX_AGE=-300)
    def test_expired_max_age_token(self):
        token = create_token(self.user)
        user = parse_token(token, self.get_user)
        self.assertEqual(user, None)
        self.assertLogsContain("Expired token")

    def test_max_age_token_without_timestamp(self):
        token = create_token(self.user)
        with override_settings(SESAME_MAX_AGE=300):
            user = parse_token(token, self.get_user)
        self.assertEqual(user, None)
        self.assertLogsContain("Valid signature but unexpected token")

    def test_token_with_timestamp(self):
        with override_settings(SESAME_MAX_AGE=300):
            token = create_token(self.user)
        user = parse_token(token, self.get_user)
        self.assertEqual(user, None)
        self.assertLogsContain("Valid signature but unexpected token")

    @override_settings(SESAME_ONE_TIME=True)
    def test_valid_one_time_token(self):
        self.test_valid_token()

    @override_settings(SESAME_ONE_TIME=True)
    def test_valid_one_time_token_when_user_never_logged_in(self):
        self.user.last_login = None
        self.user.save()
        self.test_valid_token()

    @override_settings(SESAME_ONE_TIME=True)
    def test_one_time_token_invalidation_when_last_login_date_changes(self):
        token = create_token(self.user)
        self.user.last_login = timezone.now() - datetime.timedelta(1800)
        self.user.save()
        user = parse_token(token, self.get_user)
        self.assertEqual(user, None)
        self.assertLogsContain("Invalid token")

    @override_settings(SESAME_INVALIDATE_ON_PASSWORD_CHANGE=False, SESAME_MAX_AGE=300)
    def test_no_token_invalidation_on_password_change(self):
        token = create_token(self.user)
        self.user.set_password("hunter2")
        self.user.save()
        user = parse_token(token, self.get_user)
        self.assertEqual(user, self.user)
        self.assertLogsContain("Valid token for user %s" % self.username)

    def test_naive_token_hijacking_fails(self):
        # Tokens contain the PK of the user, the hash of the revocation key,
        # and a signature. The revocation key may be identical for two users:
        # - if SESAME_INVALIDATE_ON_PASSWORD_CHANGE is False or if they don't
        #   have a password;
        # - if SESAME_ONE_TIME is False or if they have the same last_login.
        user_1 = self.user
        user_2 = self.create_user("jane", self.user.last_login)

        token1 = create_token(user_1)
        token2 = create_token(user_2)

        # Check that the test scenario produces identical revocation keys.
        # This test depends on the implementation of django.core.signing;
        # however, the format of tokens must be stable to keep them valid.
        data1, sig1 = token1.split(":", 1)
        data2, sig2 = token2.split(":", 1)
        bin_data1 = signing.b64_decode(data1.encode())
        bin_data2 = signing.b64_decode(data2.encode())
        pk1 = packer.pack_pk(user_1.pk)
        pk2 = packer.pack_pk(user_2.pk)
        self.assertEqual(bin_data1[: len(pk1)], pk1)
        self.assertEqual(bin_data2[: len(pk2)], pk2)
        key1 = bin_data1[len(pk1) :]
        key2 = bin_data2[len(pk2) :]
        self.assertEqual(key1, key2)

        # Check that changing just the PK doesn't allow hijacking the other
        # user's account -- because the PK is included in the signature.
        bin_data = pk2 + key1
        data = signing.b64_encode(bin_data).decode()
        token = data + sig1
        user = parse_token(token, self.get_user)
        self.assertEqual(user, None)
        self.assertLogsContain("Bad token")


@override_settings(AUTH_USER_MODEL="test_app.BigAutoUser")
class TestBigAutoPrimaryKey(TestTokensBase):
    pass


@override_settings(AUTH_USER_MODEL="test_app.UUIDUser")
class TestUUIDPrimaryKey(TestTokensBase):
    pass


class Packer(BasePacker):
    """
    Verbatim copy of the example in the README.

    """

    @staticmethod
    def pack_pk(user_pk):
        assert len(user_pk) == 24
        return bytes.fromhex(user_pk)

    @staticmethod
    def unpack_pk(data):
        return data[:12].hex(), data[12:]


@override_settings(
    AUTH_USER_MODEL="test_app.StrUser", SESAME_PACKER=__name__ + ".Packer",
)
class TestCustomPacker(TestTokensBase):

    username = "abcdef012345abcdef567890"

    def test_custom_packer_is_used(self):
        token = create_token(self.user)
        # base64.b64encode(bytes.fromhex(username)).decode() == "q83vASNFq83vVniQ"
        self.assertEqual(token[:16], "q83vASNFq83vVniQ")


class TestUnsupportedPrimaryKey(TestCase):
    def test_unsupported_primary_key(self):
        with self.assertRaises(NotImplementedError) as exc:
            # The exception is raised in override_settings,
            # when django-sesame initializes the tokenizer
            with override_settings(AUTH_USER_MODEL="test_app.BooleanUser"):
                assert False  # pragma: no cover

        self.assertEqual(
            str(exc.exception), "BooleanField primary keys aren't supported",
        )
