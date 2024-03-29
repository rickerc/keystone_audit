# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from __future__ import absolute_import
import copy
import datetime

import memcache

from keystone.common import utils
from keystone import config
from keystone import exception
from keystone.openstack.common import jsonutils
from keystone.openstack.common import log as logging
from keystone.openstack.common import timeutils
from keystone import token


CONF = config.CONF

LOG = logging.getLogger(__name__)


class Token(token.Driver):
    revocation_key = 'revocation-list'

    def __init__(self, client=None):
        self._memcache_client = client

    @property
    def client(self):
        return self._memcache_client or self._get_memcache_client()

    def _get_memcache_client(self):
        memcache_servers = CONF.memcache.servers.split(',')
        # NOTE(morganfainberg): The memcache client library for python is NOT
        # thread safe and should not be passed between threads. This is highly
        # specific to the cas() (compare and set) methods and the caching of
        # the previous value(s). It appears greenthread should ensure there is
        # a single data structure per spawned greenthread.
        self._memcache_client = memcache.Client(memcache_servers, debug=0,
                                                cache_cas=True)
        return self._memcache_client

    def _prefix_token_id(self, token_id):
        return 'token-%s' % token_id.encode('utf-8')

    def _prefix_user_id(self, user_id):
        return 'usertokens-%s' % user_id.encode('utf-8')

    def get_token(self, token_id):
        if token_id is None:
            raise exception.TokenNotFound(token_id='')
        ptk = self._prefix_token_id(token_id)
        token_ref = self.client.get(ptk)
        if token_ref is None:
            raise exception.TokenNotFound(token_id=token_id)

        return token_ref

    def create_token(self, token_id, data):
        data_copy = copy.deepcopy(data)
        ptk = self._prefix_token_id(token_id)
        if not data_copy.get('expires'):
            data_copy['expires'] = token.default_expire_time()
        if not data_copy.get('user_id'):
            data_copy['user_id'] = data_copy['user']['id']
        kwargs = {}
        if data_copy['expires'] is not None:
            expires_ts = utils.unixtime(data_copy['expires'])
            kwargs['time'] = expires_ts
        self.client.set(ptk, data_copy, **kwargs)
        user_id = data['user']['id']
        user_key = self._prefix_user_id(user_id)
        # Append the new token_id to the token-index-list stored in the
        # user-key within memcache.
        self._update_user_list_with_cas(user_key, token_id, data_copy)
        if CONF.trust.enabled and data.get('trust_id'):
            # NOTE(morganfainberg): If trusts are enabled and this is a trust
            # scoped token, we add the token to the trustee list as well.  This
            # allows password changes of the trustee to also expire the token.
            # There is no harm in placing the token in multiple lists, as
            # _list_tokens is smart enough to handle almost any case of
            # valid/invalid/expired for a given token.
            token_data = data_copy['token_data']
            if data_copy['token_version'] == token.provider.V2:
                trustee_user_id = token_data['access']['trust'][
                    'trustee_user_id']
            elif data_copy['token_version'] == token.provider.V3:
                trustee_user_id = token_data['OS-TRUST:trust'][
                    'trustee_user_id']
            else:
                raise token.provider.UnsupportedTokenVersionException(
                    _('Unknown token version %s') %
                    data_copy.get('token_version'))

            trustee_key = self._prefix_user_id(trustee_user_id)
            self._update_user_list_with_cas(trustee_key, token_id, data_copy)

        return copy.deepcopy(data_copy)

    def _convert_user_index_from_json(self, token_list, user_key):
        try:
            # NOTE(morganfainberg): Try loading in the old format
            # of the list.
            token_list = jsonutils.loads('[%s]' % token_list)

            # NOTE(morganfainberg): Build a delta based upon the
            # token TTL configured. Since we are using the old
            # format index-list, we will create a "fake" expiration
            # that should be further in the future than the actual
            # expiry. To avoid locking up keystone trying to
            # communicate to memcached, it is better to use a fake
            # value. The logic that utilizes this list already
            # knows how to handle the case of tokens that are
            # no longer valid being included.
            delta = datetime.timedelta(
                seconds=CONF.token.expiration)
            new_expiry = timeutils.normalize_time(
                timeutils.utcnow()) + delta

            for idx, token_id in enumerate(token_list):
                token_list[idx] = (token_id, new_expiry)

        except Exception:
            # NOTE(morganfainberg): Catch any errors thrown here. There is
            # nothing the admin or operator needs to do in this case, but
            # it should be logged that there was an error and some action was
            # taken to correct it
            LOG.info(_('Unable to convert user-token-index to new format; '
                       'clearing user token index record "%s".'), user_key)
            token_list = []
        return token_list

    def _update_user_list_with_cas(self, user_key, token_id, token_data):
        cas_retry = 0
        max_cas_retry = CONF.memcache.max_compare_and_set_retry
        current_time = timeutils.normalize_time(timeutils.utcnow())

        self.client.reset_cas()

        while cas_retry <= max_cas_retry:
            # NOTE(morganfainberg): cas or "compare and set" is a function of
            # memcache. It will return false if the value has changed since the
            # last call to client.gets(). This is the memcache supported method
            # of avoiding race conditions on set().  Memcache is already atomic
            # on the back-end and serializes operations.
            #
            # cas_retry is for tracking our iterations before we give up (in
            # case memcache is down or something horrible happens we don't
            # iterate forever trying to compare and set the new value.
            cas_retry += 1
            token_list = self.client.gets(user_key)
            filtered_list = []

            if token_list is not None:
                if not isinstance(token_list, list):
                    token_list = self._convert_user_index_from_json(token_list,
                                                                    user_key)
                for token_i, expiry in token_list:
                    expires_at = timeutils.normalize_time(expiry)
                    if expires_at < current_time:
                        # skip tokens that are expired.
                        continue

                    # Add the still valid token_id to the list.
                    filtered_list.append((token_i, expiry))
            # Add the new token_id and expiry.
            filtered_list.append(
                (token_id, timeutils.normalize_time(token_data['expires'])))

            # Use compare-and-set (cas) to set the new value for the
            # token-index-list for the user-key. Cas is used to prevent race
            # conditions from causing the loss of valid token ids from this
            # list.
            if self.client.cas(user_key, filtered_list):
                msg = _('Successful set of token-index-list for user-key '
                        '"%(user_key)s", #%(count)d records')
                LOG.debug(msg, {'user_key': user_key,
                                'count': len(filtered_list)})
                return filtered_list

            # The cas function will return true if it succeeded or false if it
            # failed for any reason, including memcache server being down, cas
            # id changed since gets() called (the data changed between when
            # this loop started and this point, etc.
            error_msg = _('Failed to set token-index-list for user-key '
                          '"%(user_key)s". Attempt %(cas_retry)d of '
                          '%(cas_retry_max)d')
            LOG.debug(error_msg,
                      {'user_key': user_key,
                       'cas_retry': cas_retry,
                       'cas_retry_max': max_cas_retry})

        # Exceeded the maximum retry attempts.
        error_msg = _('Unable to add token user list')
        raise exception.UnexpectedError(error_msg)

    def _add_to_revocation_list(self, data):
        data_json = jsonutils.dumps(data)
        if not self.client.append(self.revocation_key, ',%s' % data_json):
            if not self.client.add(self.revocation_key, data_json):
                if not self.client.append(self.revocation_key,
                                          ',%s' % data_json):
                    msg = _('Unable to add token to revocation list.')
                    raise exception.UnexpectedError(msg)

    def delete_token(self, token_id):
        # Test for existence
        data = self.get_token(token_id)
        ptk = self._prefix_token_id(token_id)
        result = self.client.delete(ptk)
        self._add_to_revocation_list(data)
        return result

    def list_tokens(self, user_id, tenant_id=None, trust_id=None,
                    consumer_id=None):
        tokens = []
        user_key = self._prefix_user_id(user_id)
        current_time = timeutils.normalize_time(timeutils.utcnow())
        token_list = self.client.get(user_key) or []
        if not isinstance(token_list, list):
            # NOTE(morganfainberg): This is for compatibility for old-format
            # token-lists that were a JSON string of just token_ids. This code
            # will reference the underlying expires directly from the
            # token_ref vs in this list, so setting to none just ensures the
            # loop works as expected.
            token_list = [(i, None) for i in
                          jsonutils.loads('[%s]' % token_list)]
        for token_id, expiry in token_list:
            ptk = self._prefix_token_id(token_id)
            token_ref = self.client.get(ptk)
            if token_ref:
                if tenant_id is not None:
                    tenant = token_ref.get('tenant')
                    if not tenant:
                        continue
                    if tenant.get('id') != tenant_id:
                        continue
                if trust_id is not None:
                    trust = token_ref.get('trust_id')
                    if not trust:
                        continue
                    if trust != trust_id:
                        continue
                if consumer_id is not None:
                    try:
                        oauth = token_ref['token_data']['token']['OS-OAUTH1']
                        if oauth.get('consumer_id') != consumer_id:
                            continue
                    except KeyError:
                        continue

                if (timeutils.normalize_time(token_ref['expires']) <
                        current_time):
                    # Skip expired tokens.
                    continue

                tokens.append(token_id)
        return tokens

    def list_revoked_tokens(self):
        list_json = self.client.get(self.revocation_key)
        if list_json:
            return jsonutils.loads('[%s]' % list_json)
        return []
