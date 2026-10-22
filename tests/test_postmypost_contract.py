import unittest
from unittest.mock import AsyncMock

from jsonschema import ValidationError

from publikot.postmypost import Postmypost
from postmypost_contract import validate_contract


class OfficialSDKContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_outgoing_publication_conforms_to_official_sdk(self):
        for images in (None, ['one.png', 'two.png', 'three.png']):
            client = Postmypost('not-a-real-token', None)
            client.upload = AsyncMock(side_effect=[800, 801, 802])
            client.request = AsyncMock(return_value={})
            await client.create({'remote_project': '10', 'remote_account': '20'}, 'Готовая публикация', images)
            payload = client.request.call_args.kwargs['json']
            validate_contract('CreatePublicationRequest', payload)
            # Reproduce the previous real failure; this must fail independently of our fakes.
            payload['details'][0]['publication_type'] = 'post'
            with self.assertRaises(ValidationError):
                validate_contract('CreatePublicationRequest', payload)

    def test_official_upload_response_uses_key_not_name(self):
        payload = {'id': 7, 'name': 'photo.png', 'size': 10, 'status': 5,
                   'action': 'https://storage.example/bucket',
                   'fields': [{'key': 'policy', 'value': 'signed-policy'}]}
        validate_contract('UploadByFile', payload)
        validate_contract('UploadComplete', {'id': 7, 'status': 3})
        validate_contract('UploadStatus', {'id': 7, 'status': 1, 'file_id': 800})
