"""Supported Exchange-capable business licenses, shared by setup and readback."""

BUSINESS_BASIC_SKUS = (
    'O365_BUSINESS_ESSENTIALS', 'SMB_BUSINESS_ESSENTIALS',
    'MICROSOFT_365_BUSINESS_BASIC_(NO TEAMS)',
    'MICROSOFT_365_BUSINESS_BASIC_(NO_TEAMS)',
    'MICROSOFT_365_BUSINESS_BASIC_EEA_(NO_TEAMS)',
    'MICROSOFT_365_BUSINESS_BASIC_EEA_(NO TEAMS)',
)
OTHER_BUSINESS_MAILBOX_SKUS = (
    'O365_BUSINESS_PREMIUM',  # Microsoft's internal name for Business Standard.
    'MICROSOFT_365_BUSINESS_STANDARD_NO_TEAMS',
    'SPB',  # Business Premium, including its trial subscription.
    'MICROSOFT_365_ BUSINESS_ PREMIUM_(NO TEAMS)',
)
ALLOWED_MAILBOX_SKUS = (*BUSINESS_BASIC_SKUS, *OTHER_BUSINESS_MAILBOX_SKUS, 'EXCHANGESTANDARD')


def powershell_sku_list(values=ALLOWED_MAILBOX_SKUS):
    return ',\n'.join('"' + value + '"' for value in values)
