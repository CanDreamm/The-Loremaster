"""Read actual image multipart uploads in local HTTP integration tests."""
import base64


async def read_generation_request(test, request):
    if request.content_type == 'application/json':
        return await request.json()
    test.assertEqual(request.content_type, 'multipart/form-data')
    reader = await request.multipart()
    payload = {}
    while part := await reader.next():
        if part.filename:
            test.assertEqual(part.name, 'image_urls[]')
            mime = part.headers['Content-Type']
            test.assertIn(mime, ('image/png', 'image/jpeg', 'image/webp'))
            test.assertTrue(part.filename.startswith('reference-'))
            raw = await part.read()
            payload.setdefault('image_urls', []).append('data:' + mime + ';base64,' + base64.b64encode(raw).decode())
        else:
            value = await part.text()
            if part.name in ('is_sync', 'translate_input'):
                test.assertIn(value, ('0', '1'))
                value = value == '1'
            elif part.name in ('width', 'height', 'num_images', 'max_images'):
                value = int(value)
            payload[part.name] = value
    return payload
