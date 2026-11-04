from unittest.mock import AsyncMock
import pytest
from app.services.graph_api import GraphAPIService
from app.services.step7_fast import _build_licensed_user_script
from app.services.objective_reconciliation import _build_mailbox_truth_script


@pytest.mark.parametrize('sku',['MICROSOFT_365_BUSINESS_STANDARD_NO_TEAMS','O365_BUSINESS_PREMIUM','SPB','Microsoft_365_ Business_ Premium_(no Teams)'])
@pytest.mark.asyncio
async def test_available_business_seat_is_accepted_by_creation_and_final_verification(sku):
    service=GraphAPIService('synthetic-token')
    service.create_user=AsyncMock(return_value={'success':True,'user':{'id':'synthetic-user'}})
    service.get_available_licenses=AsyncMock(return_value=[
        {'skuPartNumber':'SPE_E5','skuId':'unrelated-sku','prepaidUnits':{'enabled':25},'consumedUnits':0},
        {'skuPartNumber':sku,'skuId':'business-sku','appliesTo':'User','prepaidUnits':{'enabled':25},'consumedUnits':1},
    ])
    service.assign_license=AsyncMock(return_value={'success':True})
    result=await service.create_licensed_user('example.com')
    assert result['license_assigned']
    service.assign_license.assert_awaited_once_with('synthetic-user','business-sku')
    setup=_build_licensed_user_script('admin@tenant','synthetic-password','example.com','synthetic-mailbox-password')
    readback=_build_mailbox_truth_script('admin@tenant','synthetic-password','example.com',[])
    assert '"'+sku.upper()+'"' in setup and '"'+sku.upper()+'"' in readback
    assert '"SPE_E5"' not in setup and '"SPE_E5"' not in readback


@pytest.mark.asyncio
async def test_no_license_is_assigned_when_supported_seats_are_exhausted():
    service=GraphAPIService('synthetic-token')
    service.create_user=AsyncMock(return_value={'success':True,'user':{'id':'synthetic-user'}})
    service.get_available_licenses=AsyncMock(return_value=[{'skuPartNumber':'MICROSOFT_365_BUSINESS_STANDARD_NO_TEAMS','skuId':'full-sku','prepaidUnits':{'enabled':25},'consumedUnits':25}])
    service.assign_license=AsyncMock()
    result=await service.create_licensed_user('example.com')
    assert not result['license_assigned']
    service.assign_license.assert_not_awaited()
