"""All-domain edits preserve unrelated tree nodes and enforce nesting."""
from copy import deepcopy
from unittest import TestCase
from pyproj import Transformer
from schemas import Center
from domain_preview import DomainPreviewRequest, preview_domains, _projection, _origins
from test_ordinary_ui2 import tree_request


class DomainEditMatrixTest(TestCase):
    def test_all_domains_moves_and_eight_resize_handles(self):
        value = tree_request()
        payload = {k: value[k] for k in ('center', 'domains', 'configuration_profile')}
        before = preview_domains(DomainPreviewRequest.model_validate(payload))
        inverse = Transformer.from_crs(_projection(Center.model_validate(value['center'])), 'EPSG:4326', always_xy=True)
        origins = _origins(Center.model_validate(value['center']), DomainPreviewRequest.model_validate(payload).domains)
        for index, domain in enumerate(value['domains']):
            dx = value['domains'][domain['parent_id']-1]['dx'] if index else domain['dx']
            west, south = origins[index]
            east, north = west+(domain['e_we']-1)*domain['dx'], south+(domain['e_sn']-1)*domain['dy']
            edits = []
            lon, lat = inverse.transform(dx, dx)
            edits.append({'operation': 'move', 'start': value['center'], 'end': {'lon': lon, 'lat': lat}})
            for handle in ('sw','se','nw','ne','n','s','e','w'):
                x = west-dx if 'w' in handle else east+dx if 'e' in handle else (west+east)/2
                y = south-dx if 's' in handle else north+dx if 'n' in handle else (south+north)/2
                lon, lat = inverse.transform(x, y)
                edits.append({'operation':'resize','handle':handle,'end':{'lon':lon,'lat':lat}})
            for edit in edits:
                with self.subTest(domain=domain['id'], edit=edit):
                    original = deepcopy(payload)
                    after = preview_domains(DomainPreviewRequest.model_validate({**payload,'edit':{**edit,'domain_id':domain['id']}}))
                    self.assertEqual(payload, original)
                    descendants = {index+1}
                    for n, d in enumerate(value['domains'],1):
                        if d['parent_id'] in descendants: descendants.add(n)
                    for n in range(len(value['domains'])):
                        if n+1 not in descendants:
                            self.assertEqual(after['domains'][n], before['domains'][n])
                            self.assertEqual(after['boundaries'][n], before['boundaries'][n])
                    if index: self.assertEqual(after['center'],before['center'])
                    for d in after['domains'][1:]:
                        self.assertEqual((d['e_we']-1)%d['parent_grid_ratio'],0)
                        self.assertEqual((d['e_sn']-1)%d['parent_grid_ratio'],0)

    def test_outside_moves_draws_and_crossed_resize_are_rejected(self):
        value=tree_request()
        payload={k:value[k] for k in ('center','domains','configuration_profile')}
        for domain in value['domains'][1:]:
            with self.subTest(domain=domain['id']), self.assertRaises(ValueError):
                preview_domains(DomainPreviewRequest.model_validate({**payload,'edit':{
                    'operation':'move','domain_id':domain['id'],'start':value['center'],'end':{'lat':60,'lon':150}}}))
            with self.subTest(domain=domain['id'], operation='draw'), self.assertRaises(ValueError):
                preview_domains(DomainPreviewRequest.model_validate({**payload,'edit':{
                    'operation':'draw','domain_id':domain['id'],'bounds':{'west':140,'east':141,'south':55,'north':56}}}))
        initial=preview_domains(DomainPreviewRequest.model_validate(payload))
        for domain,boundary in zip(value['domains'],initial['boundaries']):
            lon,lat=boundary['corners']['sw']
            with self.subTest(domain=domain['id'], operation='resize'), self.assertRaises(ValueError):
                preview_domains(DomainPreviewRequest.model_validate({**payload,'edit':{
                    'operation':'resize','domain_id':domain['id'],'handle':'ne','end':{'lat':lat-1,'lon':lon-1}}}))
