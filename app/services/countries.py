"""Deterministic ISO 3166-1 alpha-2 country identifiers and English name aliases.

The fixed table keeps profile facts and requirement criteria comparable without a
network lookup or an additional runtime dependency. Unsupported names are rejected.
"""

from __future__ import annotations

import re
import unicodedata


def _key(value: str) -> str:
    plain = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", plain))


# Code followed by its English name and, where useful, alternate names.
_COUNTRY_NAMES = """AD|Andorra
AE|United Arab Emirates|UAE
AF|Afghanistan
AG|Antigua and Barbuda
AI|Anguilla
AL|Albania
AM|Armenia
AO|Angola
AQ|Antarctica
AR|Argentina
AS|American Samoa
AT|Austria
AU|Australia
AW|Aruba
AX|Åland Islands
AZ|Azerbaijan
BA|Bosnia and Herzegovina
BB|Barbados
BD|Bangladesh
BE|Belgium
BF|Burkina Faso
BG|Bulgaria
BH|Bahrain
BI|Burundi
BJ|Benin
BL|Saint Barthélemy
BM|Bermuda
BN|Brunei|Brunei Darussalam
BO|Bolivia|Bolivia Plurinational State of
BQ|Bonaire Sint Eustatius and Saba
BR|Brazil
BS|Bahamas|The Bahamas
BT|Bhutan
BV|Bouvet Island
BW|Botswana
BY|Belarus
BZ|Belize
CA|Canada
CC|Cocos Keeling Islands|Cocos Islands
CD|Democratic Republic of the Congo|DR Congo|Congo Democratic Republic of the
CF|Central African Republic
CG|Republic of the Congo|Congo
CH|Switzerland
CI|Côte d'Ivoire|Ivory Coast
CK|Cook Islands
CL|Chile
CM|Cameroon
CN|China|People's Republic of China
CO|Colombia
CR|Costa Rica
CU|Cuba
CV|Cabo Verde|Cape Verde
CW|Curaçao
CX|Christmas Island
CY|Cyprus
CZ|Czechia|Czech Republic
DE|Germany
DJ|Djibouti
DK|Denmark
DM|Dominica
DO|Dominican Republic
DZ|Algeria
EC|Ecuador
EE|Estonia
EG|Egypt
EH|Western Sahara
ER|Eritrea
ES|Spain
ET|Ethiopia
FI|Finland
FJ|Fiji
FK|Falkland Islands|Falkland Islands Malvinas
FM|Micronesia|Micronesia Federated States of
FO|Faroe Islands
FR|France
GA|Gabon
GB|United Kingdom|UK|Great Britain|United Kingdom of Great Britain and Northern Ireland|GBR
GD|Grenada
GE|Georgia
GF|French Guiana
GG|Guernsey
GH|Ghana
GI|Gibraltar
GL|Greenland
GM|Gambia|The Gambia
GN|Guinea
GP|Guadeloupe
GQ|Equatorial Guinea
GR|Greece
GS|South Georgia and the South Sandwich Islands
GT|Guatemala
GU|Guam
GW|Guinea-Bissau
GY|Guyana
HK|Hong Kong
HM|Heard Island and McDonald Islands
HN|Honduras
HR|Croatia
HT|Haiti
HU|Hungary
ID|Indonesia
IE|Ireland
IL|Israel
IM|Isle of Man
IN|India
IO|British Indian Ocean Territory
IQ|Iraq
IR|Iran|Iran Islamic Republic of
IS|Iceland
IT|Italy
JE|Jersey
JM|Jamaica
JO|Jordan
JP|Japan
KE|Kenya
KG|Kyrgyzstan
KH|Cambodia
KI|Kiribati
KM|Comoros
KN|Saint Kitts and Nevis
KP|North Korea|Korea Democratic People's Republic of
KR|South Korea|Republic of Korea|Korea Republic of
KW|Kuwait
KY|Cayman Islands
KZ|Kazakhstan
LA|Laos|Lao People's Democratic Republic
LB|Lebanon
LC|Saint Lucia
LI|Liechtenstein
LK|Sri Lanka
LR|Liberia
LS|Lesotho
LT|Lithuania
LU|Luxembourg
LV|Latvia
LY|Libya
MA|Morocco
MC|Monaco
MD|Moldova|Moldova Republic of
ME|Montenegro
MF|Saint Martin|Saint Martin French part
MG|Madagascar
MH|Marshall Islands
MK|North Macedonia
ML|Mali
MM|Myanmar|Burma
MN|Mongolia
MO|Macao|Macau
MP|Northern Mariana Islands
MQ|Martinique
MR|Mauritania
MS|Montserrat
MT|Malta
MU|Mauritius
MV|Maldives
MW|Malawi
MX|Mexico
MY|Malaysia
MZ|Mozambique
NA|Namibia
NC|New Caledonia
NE|Niger
NF|Norfolk Island
NG|Nigeria
NI|Nicaragua
NL|Netherlands|The Netherlands
NO|Norway
NP|Nepal
NR|Nauru
NU|Niue
NZ|New Zealand
OM|Oman
PA|Panama
PE|Peru
PF|French Polynesia
PG|Papua New Guinea
PH|Philippines|The Philippines
PK|Pakistan
PL|Poland
PM|Saint Pierre and Miquelon
PN|Pitcairn|Pitcairn Islands
PR|Puerto Rico
PS|Palestine|State of Palestine|Palestine State of
PT|Portugal
PW|Palau
PY|Paraguay
QA|Qatar
RE|Réunion
RO|Romania
RS|Serbia
RU|Russia|Russian Federation
RW|Rwanda
SA|Saudi Arabia
SB|Solomon Islands
SC|Seychelles
SD|Sudan
SE|Sweden
SG|Singapore
SH|Saint Helena Ascension and Tristan da Cunha
SI|Slovenia
SJ|Svalbard and Jan Mayen
SK|Slovakia
SL|Sierra Leone
SM|San Marino
SN|Senegal
SO|Somalia
SR|Suriname
SS|South Sudan
ST|Sao Tome and Principe|São Tomé and Príncipe
SV|El Salvador
SX|Sint Maarten|Sint Maarten Dutch part
SY|Syria|Syrian Arab Republic
SZ|Eswatini|Swaziland
TC|Turks and Caicos Islands
TD|Chad
TF|French Southern Territories
TG|Togo
TH|Thailand
TJ|Tajikistan
TK|Tokelau
TL|Timor-Leste|East Timor
TM|Turkmenistan
TN|Tunisia
TO|Tonga
TR|Türkiye|Turkey
TT|Trinidad and Tobago
TV|Tuvalu
TW|Taiwan|Taiwan Province of China
TZ|Tanzania|Tanzania United Republic of
UA|Ukraine
UG|Uganda
UM|United States Minor Outlying Islands
US|United States|United States of America|USA|U.S.|U.S.A.
UY|Uruguay
UZ|Uzbekistan
VA|Holy See|Vatican City
VC|Saint Vincent and the Grenadines
VE|Venezuela|Venezuela Bolivarian Republic of
VG|British Virgin Islands|Virgin Islands British
VI|U.S. Virgin Islands|US Virgin Islands|Virgin Islands U.S.
VN|Vietnam|Viet Nam
VU|Vanuatu
WF|Wallis and Futuna
WS|Samoa
YE|Yemen
YT|Mayotte
ZA|South Africa
ZM|Zambia
ZW|Zimbabwe"""

_COUNTRIES: dict[str, str] = {}
_NAMES: dict[str, str] = {}
for _row in _COUNTRY_NAMES.splitlines():
    _code, *_names = _row.split("|")
    for _name in [_code, *_names]:
        _COUNTRIES[_key(_name)] = _code
    for _name in _names:
        _NAMES[_key(_name)] = _code


def normalize_country(value: str) -> str:
    """Return the canonical two-letter country code, or reject unsupported input."""
    if not isinstance(value, str):
        raise ValueError("Country must be a two-letter country code or a recognized country name.")
    code = _COUNTRIES.get(_key(value))
    if code is None:
        raise ValueError("Enter a valid two-letter country code or a recognized country name.")
    return code


def words(value: str) -> list[str]:
    """The words of running text, case kept and accents dropped, for matching country names."""
    plain = unicodedata.normalize("NFKD", value)
    plain = "".join(char for char in plain if not unicodedata.combining(char))
    return re.findall(r"[^\W_]+", plain)


def country_names() -> dict[str, str]:
    """Every English name and alternate name, as lowercase words, mapped to its code.

    The bare two-letter codes are left out: in running text "in", "us" or "it" are words.
    """
    return dict(_NAMES)
