# LIGHTER / X10 API DOCUMENTATION DUMP
# SOURCE FILE: Account Index.html
## Account Index
~~~ text
Account Index
The
account index
is how Lighter identifies the wallets using integer numbers.
You can learn your account index by querying
accountsByL1Address
endpoint.
The response has
sub_accounts
as a list. The first element of the list is your main account and the index of the main account is your
account index
.
Updated 11 days ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: Account Types.html
## Account Types
~~~ text
Account Types
Premium Account (Opt-in) -- Suitable for HFT, the lowest latency on Lighter.
Fees: 0.002% Maker, 0.02% Taker
maker/cancel latency: 0ms
taker latency: 150ms
part of
volume quota program
Standard Account (Default) -- Suitable for retail and latency insensitive traders.
fees: 0 maker / 0 taker
taker latency: 300ms
maker: 200ms
cancel order: 100ms
Account Switch
You can change your Account Type (tied to your L1 address) using the
/changeAccountTier
endpoint.
You may call that endpoint if:
You have no open positions
You have no open orders
At least 24 hours have passed since the last call
Python snippet to switch tiers
:
Python: switch to premium
Python: switch to standard
import
asyncio
import
logging
import
lighter
import
requests
logging
.
basicConfig
(
level
=
logging
.
DEBUG
)
BASE_URL
=
"https://mainnet.zklighter.elliot.ai"
# You can get the values from the system_setup.py script
# API_KEY_PRIVATE_KEY =
# ACCOUNT_INDEX =
# API_KEY_INDEX =
async
def
main
():
client
=
lighter
.
SignerClient
(
url
=
BASE_URL
,
private_key
=
API_KEY_PRIVATE_KEY
,
account_index
=
ACCOUNT_INDEX
,
api_key_index
=
API_KEY_INDEX
,
)
err
=
client
.
check_client
()
if
err
is
not
None
:
print
(
f"CheckClient error:
{
err
}
"
)
return
auth
,
err
=
client
.
create_auth_token_with_expiry
(
lighter
.
SignerClient
.
DEFAULT_10_MIN_AUTH_EXPIRY
)
response
=
requests
.
post
(
f"
{
BASE_URL
}
/api/v1/changeAccountTier"
,
data
=
{
"account_index"
:
ACCOUNT_INDEX
,
"new_tier"
:
"premium"
},
headers
=
{
"Authorization"
:
auth
},
)
if
response
.
status_code
!=
200
:
print
(
f"Error:
{
response
.
text
}
"
)
return
print
(
response
.
json
())
if
__name__
==
"__main__"
:
asyncio
.
run
(
main
())
import
asyncio
import
logging
import
lighter
import
requests
logging
.
basicConfig
(
level
=
logging
.
DEBUG
)
BASE_URL
=
"https://mainnet.zklighter.elliot.ai"
# You can get the values from the system_setup.py script
# API_KEY_PRIVATE_KEY =
# ACCOUNT_INDEX =
# API_KEY_INDEX =
async
def
main
():
client
=
lighter
.
SignerClient
(
url
=
BASE_URL
,
private_key
=
API_KEY_PRIVATE_KEY
,
account_index
=
ACCOUNT_INDEX
,
api_key_index
=
API_KEY_INDEX
,
)
err
=
client
.
check_client
()
if
err
is
not
None
:
print
(
f"CheckClient error:
{
err
}
"
)
return
auth
,
err
=
client
.
create_auth_token_with_expiry
(
lighter
.
SignerClient
.
DEFAULT_10_MIN_AUTH_EXPIRY
)
response
=
requests
.
post
(
f"
{
BASE_URL
}
/api/v1/changeAccountTier"
,
data
=
{
"account_index"
:
ACCOUNT_INDEX
,
"new_tier"
:
"standard"
},
headers
=
{
"Authorization"
:
auth
},
)
if
response
.
status_code
!=
200
:
print
(
f"Error:
{
response
.
text
}
"
)
return
print
(
response
.
json
())
if
__name__
==
"__main__"
:
asyncio
.
run
(
main
())
How fees are collected:
In isolated margin, fees are taken from the isolated position itself, but if needed, we automatically transfer from cross margin to keep the position healthy. In cross margin, fees are always deducted directly from the available cross balance.
Sub-accounts share the same tier as the main L1 address on the account. Youâ€™ll be able to switch to a Premium Account now. Let us know if you have any questions.
Updated 11 days ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: account.html
## account
~~~ text
account
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
by
string
enum
required
index
l1_address
Allowed:
index
l1_address
value
string
required
200
A successful response.
object
code
int32
required
message
string
total
int64
required
accounts
array of objects
required
accounts
*
object
code
int32
required
message
string
account_type
uint8
required
index
int64
required
l1_address
string
required
cancel_all_time
int64
required
total_order_count
int64
required
total_isolated_order_count
int64
required
pending_order_count
int64
required
available_balance
string
required
status
uint8
required
collateral
string
required
account_index
int64
required
name
string
required
description
string
required
can_invite
boolean
required
Remove After FE uses L1 meta endpoint
referral_points_percentage
string
required
Remove After FE uses L1 meta endpoint
positions
array of objects
required
positions
*
object
market_id
uint8
required
symbol
string
required
initial_margin_fraction
string
required
open_order_count
int64
required
pending_order_count
int64
required
position_tied_order_count
int64
required
sign
int32
required
position
string
required
avg_entry_price
string
required
position_value
string
required
unrealized_pnl
string
required
realized_pnl
string
required
liquidation_price
string
required
total_funding_paid_out
string
margin_mode
int32
required
allocated_margin
string
required
total_asset_value
string
required
cross_asset_value
string
required
pool_info
object
required
PublicPoolInfo object
shares
array of objects
required
shares
*
object
public_pool_index
int64
required
shares_amount
int64
required
entry_usdc
string
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
'https://mainnet.zklighter.elliot.ai/api/v1/account?by=index'
\
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: accountActiveOrders.html
## accountActiveOrders
~~~ text
accountActiveOrders
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
account_index
int64
required
market_id
uint8
required
auth
string
made optional to support header auth clients
authorization
string
make required after integ is done
200
A successful response.
object
code
int32
required
message
string
next_cursor
string
orders
array of objects
required
orders
*
object
order_index
int64
required
client_order_index
int64
required
order_id
string
required
client_order_id
string
required
market_index
uint8
required
owner_account_index
int64
required
initial_base_amount
string
required
price
string
required
nonce
int64
required
remaining_base_amount
string
required
is_ask
boolean
required
base_size
int64
required
base_price
int32
required
filled_base_amount
string
required
filled_quote_amount
string
required
side
string
required
Defaults to buy
TODO: remove this
type
string
enum
required
limit
market
stop-loss
stop-loss-limit
take-profit
take-profit-limit
twap
twap-sub
liquidation
time_in_force
string
enum
required
Defaults to good-till-time
good-till-time
immediate-or-cancel
post-only
Unknown
reduce_only
boolean
required
trigger_price
string
required
order_expiry
int64
required
status
string
enum
required
in-progress
pending
open
filled
canceled
canceled-post-only
canceled-reduce-only
canceled-position-not-allowed
canceled-margin-not-allowed
canceled-too-much-slippage
canceled-not-enough-liquidity
canceled-self-trade
canceled-expired
canceled-oco
canceled-child
canceled-liquidation
trigger_status
string
enum
required
na
ready
mark-price
twap
parent-order
trigger_time
int64
required
parent_order_index
int64
required
parent_order_id
string
required
to_trigger_order_id_0
string
required
to_trigger_order_id_1
string
required
to_cancel_order_id_0
string
required
block_height
int64
required
timestamp
int64
required
created_at
int64
required
updated_at
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/accountActiveOrders \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: accountInactiveOrders.html
## accountInactiveOrders
~~~ text
accountInactiveOrders
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
auth
string
made optional to support header auth clients
account_index
int64
required
market_id
uint8
Defaults to 255
ask_filter
int8
Defaults to -1
between_timestamps
string
cursor
string
limit
int64
required
1 to 100
authorization
string
make required after integ is done
200
A successful response.
object
code
int32
required
message
string
next_cursor
string
orders
array of objects
required
orders
*
object
order_index
int64
required
client_order_index
int64
required
order_id
string
required
client_order_id
string
required
market_index
uint8
required
owner_account_index
int64
required
initial_base_amount
string
required
price
string
required
nonce
int64
required
remaining_base_amount
string
required
is_ask
boolean
required
base_size
int64
required
base_price
int32
required
filled_base_amount
string
required
filled_quote_amount
string
required
side
string
required
Defaults to buy
TODO: remove this
type
string
enum
required
limit
market
stop-loss
stop-loss-limit
take-profit
take-profit-limit
twap
twap-sub
liquidation
time_in_force
string
enum
required
Defaults to good-till-time
good-till-time
immediate-or-cancel
post-only
Unknown
reduce_only
boolean
required
trigger_price
string
required
order_expiry
int64
required
status
string
enum
required
in-progress
pending
open
filled
canceled
canceled-post-only
canceled-reduce-only
canceled-position-not-allowed
canceled-margin-not-allowed
canceled-too-much-slippage
canceled-not-enough-liquidity
canceled-self-trade
canceled-expired
canceled-oco
canceled-child
canceled-liquidation
trigger_status
string
enum
required
na
ready
mark-price
twap
parent-order
trigger_time
int64
required
parent_order_index
int64
required
parent_order_id
string
required
to_trigger_order_id_0
string
required
to_trigger_order_id_1
string
required
to_cancel_order_id_0
string
required
block_height
int64
required
timestamp
int64
required
created_at
int64
required
updated_at
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/accountInactiveOrders \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: accountLimits.html
## accountLimits
~~~ text
accountLimits
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
account_index
int64
required
auth
string
made optional to support header auth clients
authorization
string
make required after integ is done
200
A successful response.
object
code
int32
required
message
string
max_llp_percentage
int32
required
user_tier
string
required
can_create_public_pool
boolean
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/accountLimits \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: accountMetadata.html
## accountMetadata
~~~ text
accountMetadata
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
by
string
enum
required
index
l1_address
Allowed:
index
l1_address
value
string
required
auth
string
authorization
string
200
A successful response.
object
code
int32
required
message
string
account_metadatas
array of objects
required
account_metadatas
*
object
account_index
int64
required
name
string
required
description
string
required
can_invite
boolean
required
Remove After FE uses L1 meta endpoint
referral_points_percentage
string
required
Remove After FE uses L1 meta endpoint
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
'https://mainnet.zklighter.elliot.ai/api/v1/accountMetadata?by=index'
\
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: accountsByL1Address.html
## accountsByL1Address
~~~ text
accountsByL1Address
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
l1_address
string
required
200
A successful response.
object
code
int32
required
message
string
l1_address
string
required
sub_accounts
array of objects
required
sub_accounts
*
object
code
int32
required
message
string
account_type
uint8
required
index
int64
required
l1_address
string
required
cancel_all_time
int64
required
total_order_count
int64
required
total_isolated_order_count
int64
required
pending_order_count
int64
required
available_balance
string
required
status
uint8
required
collateral
string
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/accountsByL1Address \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: announcement.html
## announcement
~~~ text
announcement
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
200
A successful response.
object
code
int32
required
message
string
announcements
array of objects
required
announcements
*
object
title
string
required
content
string
required
created_at
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/announcement \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: apikeys.html
## apikeys
~~~ text
apikeys
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
account_index
int64
required
api_key_index
uint8
Defaults to 255
200
A successful response.
object
code
int32
required
message
string
api_keys
array of objects
required
api_keys
*
object
account_index
int64
required
api_key_index
uint8
required
nonce
int64
required
public_key
string
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/apikeys \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: block.html
## block
~~~ text
block
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
by
string
enum
required
commitment
height
Allowed:
commitment
height
value
string
required
200
A successful response.
object
code
int32
required
message
string
total
int64
required
blocks
array of objects
required
blocks
*
object
commitment
string
required
height
int64
required
state_root
string
required
priority_operations
int32
required
on_chain_l2_operations
int32
required
pending_on_chain_operations_pub_data
string
required
committed_tx_hash
string
required
committed_at
int64
required
verified_tx_hash
string
required
verified_at
int64
required
txs
array of objects
required
txs
*
object
hash
string
required
type
uint8
required
1 to 64
info
string
required
event_info
string
required
status
int64
required
transaction_index
int64
required
l1_address
string
required
account_index
int64
required
nonce
int64
required
expire_at
int64
required
block_height
int64
required
queued_at
int64
required
executed_at
int64
required
sequence_index
int64
required
parent_hash
string
required
status
int64
required
size
integer
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
'https://mainnet.zklighter.elliot.ai/api/v1/block?by=commitment'
\
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: blocks.html
## blocks
~~~ text
blocks
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
index
int64
limit
int64
required
1 to 100
sort
string
enum
Defaults to asc
asc
desc
Allowed:
asc
desc
200
A successful response.
object
code
int32
required
message
string
total
int64
required
blocks
array of objects
required
blocks
*
object
commitment
string
required
height
int64
required
state_root
string
required
priority_operations
int32
required
on_chain_l2_operations
int32
required
pending_on_chain_operations_pub_data
string
required
committed_tx_hash
string
required
committed_at
int64
required
verified_tx_hash
string
required
verified_at
int64
required
txs
array of objects
required
txs
*
object
hash
string
required
type
uint8
required
1 to 64
info
string
required
event_info
string
required
status
int64
required
transaction_index
int64
required
l1_address
string
required
account_index
int64
required
nonce
int64
required
expire_at
int64
required
block_height
int64
required
queued_at
int64
required
executed_at
int64
required
sequence_index
int64
required
parent_hash
string
required
status
int64
required
size
integer
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/blocks \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: blockTxs.html
## blockTxs
~~~ text
blockTxs
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
by
string
enum
required
block_height
block_commitment
Allowed:
block_height
block_commitment
value
string
required
200
A successful response.
object
code
int32
required
message
string
txs
array of objects
required
txs
*
object
hash
string
required
type
uint8
required
1 to 64
info
string
required
event_info
string
required
status
int64
required
transaction_index
int64
required
l1_address
string
required
account_index
int64
required
nonce
int64
required
expire_at
int64
required
block_height
int64
required
queued_at
int64
required
executed_at
int64
required
sequence_index
int64
required
parent_hash
string
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
'https://mainnet.zklighter.elliot.ai/api/v1/blockTxs?by=block_height'
\
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: candlesticks.html
## candlesticks
~~~ text
candlesticks
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
market_id
uint8
required
resolution
string
enum
required
1m
5m
15m
30m
1h
4h
12h
1d
1w
Show 9 enum values
start_timestamp
int64
required
0 to 5000000000000
end_timestamp
int64
required
0 to 5000000000000
count_back
int64
required
set_timestamp_to_end
boolean
Defaults to false
true
false
200
A successful response.
object
code
int32
required
message
string
resolution
string
required
candlesticks
array of objects
required
candlesticks
*
object
timestamp
int64
required
open
double
required
high
double
required
low
double
required
close
double
required
volume0
double
required
volume1
double
required
last_trade_id
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
'https://mainnet.zklighter.elliot.ai/api/v1/candlesticks?resolution=1m'
\
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: changeAccountTier.html
## changeAccountTier
~~~ text
changeAccountTier
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
auth
string
made optional to support header auth clients
account_index
int64
required
new_tier
string
required
authorization
string
make required after integ is done
200
A successful response.
object
code
int32
required
message
string
400
Bad request
object
code
int32
required
message
string
Updated 9 days ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
POST \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/changeAccountTier \
3
--header
'accept: application/json'
\
4
--header
'content-type: multipart/form-data'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 9 days ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: currentHeight.html
## currentHeight
~~~ text
currentHeight
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
200
A successful response.
object
code
int32
required
message
string
height
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/currentHeight \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: Data Structures, Constants and Errors.html
## Data Structures, Constants and Errors
~~~ text
Data Structures, Constants and Errors
Data and Event Structures
Go
type
Order
struct
{
OrderIndex
int64
`json:"i"`
ClientOrderIndex
int64
`json:"u"`
OwnerAccountId
int64
`json:"a"`
InitialBaseAmount
int64
`json:"is"`
Price
uint32
`json:"p"`
RemainingBaseAmount
int64
`json:"rs"`
IsAsk
uint8
`json:"ia"`
Type
uint8
`json:"ot"`
TimeInForce
uint8
`json:"f"`
ReduceOnly
uint8
`json:"ro"`
TriggerPrice
uint32
`json:"tp"`
Expiry
int64
`json:"e"`
Status
uint8
`json:"st"`
TriggerStatus
uint8
`json:"ts"`
ToTriggerOrderIndex0
int64
`json:"t0"`
ToTriggerOrderIndex1
int64
`json:"t1"`
ToCancelOrderIndex0
int64
`json:"c0"`
}
type
CancelOrder
struct
{
AccountId
int64
`json:"a"`
OrderIndex
int64
`json:"i"`
ClientOrderIndex
int64
`json:"u"`
AppError
string
`json:"ae"`
}
type
ModifyOrder
struct
{
MarketId
uint8
`json:"m"`
OldOrder
*
Order
`json:"oo"`
NewOrder
*
Order
`json:"no"`
AppError
string
`json:"ae"`
}
type
Trade
struct
{
Price
uint32
`json:"p"`
Size
int64
`json:"s"`
TakerFee
int32
`json:"tf"`
MakerFee
int32
`json:"mf"`
}
Constants
Go
TxTypeL2ChangePubKey
=
8
TxTypeL2CreateSubAccount
=
9
TxTypeL2CreatePublicPool
=
10
TxTypeL2UpdatePublicPool
=
11
TxTypeL2Transfer
=
12
TxTypeL2Withdraw
=
13
TxTypeL2CreateOrder
=
14
TxTypeL2CancelOrder
=
15
TxTypeL2CancelAllOrders
=
16
TxTypeL2ModifyOrder
=
17
TxTypeL2MintShares
=
18
TxTypeL2BurnShares
=
19
TxTypeL2UpdateLeverage
=
20
Transaction Status Mapping
Go
0
:
Failed
1
:
Pending
2
:
Executed
3
:
Pending
-
Final
State
Error Codes
Go
// Tx
AppErrTxNotFound
=
NewBusinessError
(
21500
,
"transaction not found"
)
AppErrInvalidTxInfo
=
NewBusinessError
(
21501
,
"invalid tx info"
)
AppErrMarshalTxFailed
=
NewBusinessError
(
21502
,
"marshal tx failed"
)
AppErrMarshalEventsFailed
=
NewBusinessError
(
21503
,
"marshal event failed"
)
AppErrFailToL1Signature
=
NewBusinessError
(
21504
,
"fail to l1 signature"
)
AppErrUnsupportedTxType
=
NewBusinessError
(
21505
,
"unsupported tx type"
)
AppErrTooManyTxs
=
NewBusinessError
(
21506
,
"too many pending txs. Please try again later"
)
AppErrAccountBelowMaintenanceMargin
=
NewBusinessError
(
21507
,
"account is below maintenance margin, can't execute transaction"
)
AppErrAccountBelowInitialMargin
=
NewBusinessError
(
21508
,
"account is below initial margin, can't execute transaction"
)
AppErrInvalidTxTypeForAccount
=
NewBusinessError
(
21511
,
"invalid tx type for account"
)
AppErrInvalidL1RequestId
=
NewBusinessError
(
21512
,
"invalid l1 request id"
)
// OrderBook
AppErrInactiveCancel
=
NewBusinessError
(
21600
,
"given order is not an active limit order"
)
AppErrOrderBookFull
=
NewBusinessError
(
21601
,
"order book is full"
)
AppErrInvalidMarketIndex
=
NewBusinessError
(
21602
,
"invalid market index"
)
AppErrInvalidMinAmountsForMarket
=
NewBusinessError
(
21603
,
"invalid min amounts for market"
)
AppErrInvalidMarginFractionsForMarket
=
NewBusinessError
(
21604
,
"invalid margin fractions for market"
)
AppErrInvalidMarketStatus
=
NewBusinessError
(
21605
,
"invalid market status"
)
AppErrMarketAlreadyExist
=
NewBusinessError
(
21606
,
"market already exist for given index"
)
AppErrInvalidMarketFees
=
NewBusinessError
(
21607
,
"invalid market fees"
)
AppErrInvalidQuoteMultiplier
=
NewBusinessError
(
21608
,
"invalid quote multiplier"
)
AppErrInvalidInterestRate
=
NewBusinessError
(
21611
,
"invalid interest rate"
)
AppErrInvalidOpenInterest
=
NewBusinessError
(
21612
,
"invalid open interest"
)
AppErrInvalidMarginMode
=
NewBusinessError
(
21613
,
"invalid margin mode"
)
AppErrNoPositionFound
=
NewBusinessError
(
21614
,
"no position found"
)
// Order
AppErrInvalidOrderIndex
=
NewBusinessError
(
21700
,
"invalid order index"
)
AppErrInvalidBaseAmount
=
NewBusinessError
(
21701
,
"invalid base amount"
)
AppErrInvalidPrice
=
NewBusinessError
(
21702
,
"invalid price"
)
AppErrInvalidIsAsk
=
NewBusinessError
(
21703
,
"invalid isAsk"
)
AppErrInvalidOrderType
=
NewBusinessError
(
21704
,
"invalid OrderType"
)
AppErrInvalidOrderTimeInForce
=
NewBusinessError
(
21705
,
"invalid OrderTimeInForce"
)
AppErrInvalidOrderAmount
=
NewBusinessError
(
21706
,
"invalid order base or quote amount"
)
AppErrInvalidOrderOwner
=
NewBusinessError
(
21707
,
"account is not owner of the order"
)
AppErrEmptyOrder
=
NewBusinessError
(
21708
,
"order is empty"
)
AppErrInactiveOrder
=
NewBusinessError
(
21709
,
"order is inactive"
)
AppErrUnsupportedOrderType
=
NewBusinessError
(
21710
,
"unsupported order type"
)
AppErrInvalidOrderExpiry
=
NewBusinessError
(
21711
,
"invalid expiry"
)
AppErrAccountHasAQueuedCancelAllOrdersRequest
=
NewBusinessError
(
21712
,
"account has a queued cancel all orders request"
)
AppErrInvalidCancelAllTimeInForce
=
NewBusinessError
(
21713
,
"invalid cancel all time in force"
)
AppErrInvalidCancelAllTime
=
NewBusinessError
(
21714
,
"invalid cancel all time"
)
AppErrInctiveOrder
=
NewBusinessError
(
21715
,
"given order is not an active order"
)
AppErrOrderNotExpired
=
NewBusinessError
(
21716
,
"order is not expired"
)
AppErrMaxOrdersPerAccount
=
NewBusinessError
(
21717
,
"maximum active limit order count reached"
)
AppErrMaxOrdersPerAccountPerMarket
=
NewBusinessError
(
21718
,
"maximum active limit order count per market reached"
)
AppErrMaxPendingOrdersPerAccount
=
NewBusinessError
(
21719
,
"maximum pending order count reached"
)
AppErrMaxPendingOrdersPerAccountPerMarket
=
NewBusinessError
(
21720
,
"maximum pending order count per market reached"
)
AppErrMaxTWAPOrdersInExchange
=
NewBusinessError
(
21721
,
"maximum twap order count reached"
)
AppErrMaxConditionalOrdersInExchange
=
NewBusinessError
(
21722
,
"maximum conditional order count reached"
)
AppErrInvalidAccountHealth
=
NewBusinessError
(
21723
,
"invalid account health"
)
AppErrInvalidLiquidationSize
=
NewBusinessError
(
21724
,
"invalid liquidation size"
)
AppErrInvalidLiquidationPrice
=
NewBusinessError
(
21725
,
"invalid liquidation price"
)
AppErrInsuranceFundCannotBePartiallyLiquidated
=
NewBusinessError
(
21726
,
"insurance fund cannot be partially liquidated"
)
AppErrInvalidClientOrderIndex
=
NewBusinessError
(
21727
,
"invalid client order index"
)
AppErrClientOrderIndexExists
=
NewBusinessError
(
21728
,
"client order index already exists"
)
AppErrInvalidOrderTriggerPrice
=
NewBusinessError
(
21729
,
"invalid order trigger price"
)
AppOrderStatusIsNotPending
=
NewBusinessError
(
21730
,
"order status is not pending"
)
AppPendingOrderCanNotBeTriggered
=
NewBusinessError
(
21731
,
"order can not be triggered"
)
AppReduceOnlyIncreasesPosition
=
NewBusinessError
(
21732
,
"reduce only increases position"
)
AppErrFatFingerPrice
=
NewBusinessError
(
21733
,
"order price flagged as an accidental price"
)
AppErrPriceTooFarFromMarkPrice
=
NewBusinessError
(
21734
,
"limit order price is too far from the mark price"
)
AppErrPriceTooFarFromTrigger
=
NewBusinessError
(
21735
,
"SL/TP order price is too far from the trigger price"
)
AppErrInvalidOrderTriggerStatus
=
NewBusinessError
(
21736
,
"invalid order trigger status"
)
AppErrInvalidOrderStatus
=
NewBusinessError
(
21737
,
"invalid order status"
)
AppErrInvalidReduceOnlyDirection
=
NewBusinessError
(
21738
,
"invalid reduce only direction"
)
AppErrNotEnoughOrderMargin
=
NewBusinessError
(
21739
,
"not enough margin to create the order"
)
AppErrInvalidReduceOnlyMode
=
NewBusinessError
(
21740
,
"invalid reduce only mode"
)
// Deleverage
AppErrDeleverageAgainstItself
=
NewBusinessError
(
21901
,
"deleverage against itself"
)
AppErrDeleverageDoesNotMatchLiquidationStatus
=
NewBusinessError
(
21902
,
"deleverage does not match liquidation status"
)
AppErrDeleverageWithOpenOrders
=
NewBusinessError
(
21903
,
"deleverage with open orders"
)
AppErrInvalidDeleverageSize
=
NewBusinessError
(
21904
,
"invalid deleverage size"
)
AppErrInvalidDeleveragePrice
=
NewBusinessError
(
21905
,
"invalid deleverage price"
)
AppErrInvalidDeleverageSide
=
NewBusinessError
(
21906
,
"invalid deleverage side"
)
// RateLimit
AppErrTooManyRequest
=
NewBusinessError
(
23000
,
"Too Many Requests!"
)
AppErrTooManySubscriptions
=
NewBusinessError
(
23001
,
"Too Many Subscriptions!"
)
AppErrTooManyDifferentAccounts
=
NewBusinessError
(
23002
,
"Too Many Different Accounts!"
)
AppErrTooManyConnections
=
NewBusinessError
(
23003
,
"Too Many Connections!"
)
Updated about 1 month ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: deposit\_history.html
## deposit\_history
~~~ text
deposit_history
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
account_index
int64
required
auth
string
made optional to support header auth clients
l1_address
string
required
cursor
string
filter
string
enum
all
pending
claimable
Allowed:
all
pending
claimable
authorization
string
make required after integ is done
200
A successful response.
object
code
int32
required
message
string
deposits
array of objects
required
deposits
*
object
id
string
required
amount
string
required
timestamp
int64
required
status
string
enum
required
failed
pending
completed
claimable
l1_tx_hash
string
required
cursor
string
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/deposit/history \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: exchangeStats.html
## exchangeStats
~~~ text
exchangeStats
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
200
A successful response.
object
code
int32
required
message
string
total
int64
required
order_book_stats
array of objects
required
order_book_stats
*
object
symbol
string
required
last_trade_price
double
required
daily_trades_count
int64
required
daily_base_token_volume
double
required
daily_quote_token_volume
double
required
daily_price_change
double
required
daily_usd_volume
double
required
daily_trades_count
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/exchangeStats \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: export.html
## export
~~~ text
export
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
auth
string
account_index
int64
Defaults to -1
market_id
uint8
Defaults to 255
type
string
enum
required
funding
trade
Allowed:
funding
trade
authorization
string
200
A successful response.
object
code
int32
required
message
string
data_url
string
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
'https://mainnet.zklighter.elliot.ai/api/v1/export?type=funding'
\
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: Extended API Documentation â€“ API Documentation.html
## Extended API Documentation â€“ API Documentation
~~~ text
Extended API Documentation â€“ API Documentation
NAV
json
Extended API Documentation
Introduction
StarkEx to Starknet migration
Python SDK
Mainnet
Testnet
Allowed HTTP Verbs
Authentication
Rate Limits
Pagination
Public REST-API
Get markets
Get market statistics
Get market order book
Get market last trades
Get candles history
Get funding rates history
Get open interest history
Private REST-API
Account
Get account details
Get balance
Get deposits, withdrawals, transfers history
Get positions
Get positions history
Get open orders
Get orders history
Get order by id
Get orders by external id
Get trades
Get funding payments
Get rebates
Get current leverage
Update leverage
Get fees
Order management
Create or edit order
Cancel order by ID
Cancel order by external id
Mass Cancel
Mass auto-cancel (dead man's switch)
Bridge Config
Get bridge quote
Commit quote
Deposits
Withdrawals
Create transfer
Referrals
Get affiliate data
Get referral status
Get referral links
Get referral dashboard
Use referral link
Create referral link code
Update referral link code
Points
Get Earned Points
Get points leaderboard stats
Points league levels
Public WebSocket streams
Order book stream
Trades stream
Funding rates stream
Candles stream
Mark price stream
Index price stream
Private WebSocket streams
Account updates stream
Error responses
Legacy: StarkEx SDK
Extended API Documentation
By using the Extended API, you agree to the
Extended Terms
&
Privacy Policy
. If you do not agree to the foregoing terms, do not use the Extended API.
Introduction
This documentation is a work in progress and will be updated regularly based on user feedback and the addition of new functionality.
Welcome to the Extended API Documentation! This guide is designed to assist traders and developers in integrating with our hybrid perpetuals exchange.
Extended operates as a hybrid Central Limit Order Book (CLOB) exchange. While order processing, matching, position risk assessment, and transaction sequencing are handled off-chain, trade settlement occurs on-chain via Starknet.
Extended is designed to operate in a completely trustless manner, enabled by two core principles:
Users retain self-custody of their funds, with all assets held in smart contracts on Starknet. This means Extended has no custodial access to user assets under any circumstances.
On-chain validation of the trading logic ensures that fraudulent or incorrect transactions, including liquidations that are contrary to the on-chain rules, are never permitted.
All transactions that happen on Extended are settled on Starknet. While Starknet does not rely on Ethereum Layer 1 for every individual transaction, it inherits Ethereumâ€™s security by publishing zero-knowledge proofs every few hours. These proofs validate state transitions on Starknet, ensuring the integrity and correctness of the entire system.
Extended's on-chain logic and smart contracts have undergone extensive audits by external security firms. The audit reports are available below:
ChainSecurity
.
Public audit competition
.
For a deeper breakdown of the core principles that make Extended trustless, see the blog
Why Safe
. For more on Extended Exchange's roadmap and architecture, check out
Extended Vision
and
Architecture
, respectively.
To optimize high-frequency trading performance, the Extended API operates asynchronously. When you place an order, it immediately returns an order ID, even before the order is officially recorded in the book. To track your order status in real time subscribe to the Order WebSocket stream, which delivers instant updates on confirmations, cancellations, and rejections.
StarkEx to Starknet migration
On August 12, 2025, Extended began the migration from StarkEx to Starknet. This transition marks the first step toward our long-term vision of the Extended ecosystem and the introduction of unified margin. You can read more about the broader migration rationale and vision in our
documentation
.
Existing Extended users will need to migrate from the current StarkEx instance to the new Starknet instance. The migration process has been designed to be as seamless as possible and is explained
here
. New users will be onboarded directly to the Starknet instance.
For the Starknet instance of the platform, the following changes vs StarkEx apply:
Wallet support: In addition to EVM-compatible wallets, we will also support Starknet-compatible wallets.
Signing logic: New signing logic in line with the SNIP12 standard (EIP712 for Starknet) and examples are available via the
SDK
.
Deposits and withdrawals: For EVM wallets, we support deposits and withdrawals on six major EVM chains, currently only via the user interface. For Starknet wallets, deposits and withdrawals via Starknet are now supported.
URL: The URL for the Starknet instance is api.starknet.extended.exchange, vs. api.extended.exchange for the StarkEx instance.
The migration will be rolled out in three stages:
Stage 1 â€“ Dual Operation Mode,
Stage 2 â€“ StarkEx Wind-Down Mode,
Stage 3 â€“ StarkEx Freeze.
While the StarkEx instance will remain fully operational during Stage 1 of the migration, certain restrictions will apply starting August 12. Please review them carefully
here
.
Until the migration is complete, all StarkEx-specific details can be found in the dedicated section of the
API documentation
.
Python SDK
SDK configuration
Copy to Clipboard
from dataclasses import dataclass
@dataclass
class EndpointConfig:
chain_rpc_url: str
api_base_url: str
stream_url: str
onboarding_url: str
signing_domain: str
collateral_asset_contract: str
asset_operations_contract: str
collateral_asset_on_chain_id: str
collateral_decimals: int
STARKNET_TESTNET_CONFIG = EndpointConfig(
chain_rpc_url="https://rpc.sepolia.org",
api_base_url="https://api.starknet.sepolia.extended.exchange/api/v1",
stream_url="wss://starknet.sepolia.extended.exchange/stream.extended.exchange/v1",
onboarding_url="https://api.starknet.sepolia.extended.exchange",
signing_domain="starknet.sepolia.extended.exchange",
collateral_asset_contract="",
asset_operations_contract="",
collateral_asset_on_chain_id="",
collateral_decimals=6,
starknet_domain=StarknetDomain(name="Perpetuals", version="v0", chain_id="SN_SEPOLIA", revision="1"),
collateral_asset_id="0x1",
)
STARKNET_MAINNET_CONFIG = EndpointConfig(
chain_rpc_url="",
api_base_url="https://api.starknet.extended.exchange/api/v1",
stream_url="wss://api.starknet.extended.exchange/stream.extended.exchange/v1",
onboarding_url="https://api.starknet.extended.exchange",
signing_domain="extended.exchange",
collateral_asset_contract="",
asset_operations_contract="",
collateral_asset_on_chain_id="0x1",
collateral_decimals=6,
starknet_domain=StarknetDomain(name="Perpetuals", version="v0", chain_id="SN_MAIN", revision="1"),
collateral_asset_id="0x1",
)
The SDK and the SDK documentation will be updated regularly to include additional functionality and more examples.
Getting Started:
For installation instructions, please refer to the
description
provided.
For reference implementations, explore the
examples folder
.
For SDK configuration, please refer to the
config description
.
Supported Features:
Account creation and authorisation.
Order Management.
Account Management.
Transfers.
Withdrawals (for Starknet wallets only).
Market Information.
We are committed to enhancing the SDK with more functionalities based on user feedback and evolving market needs.
Mainnet
Our Mainnet is running on
Starknet
.
Base URL for the Mainnet API endpoints: https://api.starknet.extended.exchange/.
UI URL:
https://app.extended.exchange/perp
.
Testnet
Our Testnet is running on
Sepolia
.
Base URL for the Testnet API endpoints: https://api.starknet.sepolia.extended.exchange/.
UI URL:
https://starknet.sepolia.extended.exchange/perp
On the testnet, users can claim $1,000 worth of test USDC per hour for each wallet. This can be done by clicking the 'Claim' button in the 'Account' section, located at the bottom right of the Extended Testnet Trade screen.
Allowed HTTP Verbs
GET
: Retrieves a resource or list of resources.
POST
: Creates a resource.
PATCH
: Updates a resource.
DELETE
: Deletes a resource.
Authentication
Due to the trustless, self-custody nature of the Extended exchange, transactions involving user funds require both an API key and a valid Stark signature.
For order management, both an API key and Stark signature are necessary. For other endpoints, only the API key signature is required. Stark signatures are generated using a private Stark key.
Account Creation, API and Stark Key Management
Currently, accounts can be created through the SDK or the User Interface:
SDK - refer to the
onboarding example
.
User Interface - connect your wallet on
extended.exchange
to create your Extended account.
You can create up to ten Extended sub-accounts per one wallet address. You can add and manage all sub-accounts associated with your connected wallet in the 'Account' section, located at the bottom right of the
Extended Trade screen
.
On the
API management
page, you can obtain API keys, Stark keys, and Vault numbers for each of your sub-accounts. Note that each sub-account is a separate Starknet position and therefore has unique API and Stark keys.
Authenticate Using API Key
Extended uses a simplified authentication scheme for API access. Include your API key in the HTTP header as follows:
X-Api-Key: <API_KEY_FROM_API_MANAGEMENT_PAGE_OF_UI>
.
Mandatory headers
For both REST and WebSocket API requests, the
User-Agent
header is required.
Rate Limits
REST API endpoints are subject to rate limits. For real-time data, consider using the WebSockets API instead.
All REST API endpoints are throttled by IP address. Currently, the rate limit is set at 1,000 requests per minute, shared across all endpoints. We plan to increase these limits as our system expands. If you require an increase in the rate limit now, please reach out to our team on
Discord
.
Higher rate limit of 60,000 requests per 5 minutes apply for the market makers.
When a REST API rate limit is exceeded, a 429 status code will be returned.
Pagination
Paginated response schema:
Copy to Clipboard
type
PaginatedResponse
=
{
"
status
"
:
"
ok
"
|
"
error
"
"
data
"
:
object
|
object
[]
|
string
|
number
,
"
error
"
:
{
"
code
"
:
number
,
"
message
"
:
string
},
"
pagination
"
:
{
"
cursor
"
:
number
// Current cursor
"
count
"
:
number
// Count of the items in the response
}
}
General not paginated response schema:
Copy to Clipboard
type
GeneralResponse
=
{
"
status
"
:
"
ok
"
|
"
error
"
,
"
data
"
:
object
|
object
[]
|
string
|
number
,
"
error
"
:
{
"
code
"
:
number
,
"
message
"
:
string
}
}
The Extended API uses a cursor-based pagination model across all endpoints that may return large volumes of items.
Items are automatically sorted in descending order by ID unless otherwise specified in the endpoint description. As IDs increase over time, the most recent items are always returned first.
Pagination parameters are passed via the query string. These parameters include:
Parameter
Required
Type
Description
cursor
no
number
Determines the offset of the returned result. It represents the ID of the item after which you want to retrieve the next result. To get the next result page, use the cursor from the pagination section of the previous response.
limit
no
number
The maximum number of items that should be returned.
Public REST-API
The following Public REST API endpoints enable users to access comprehensive information about available markets, their configurations, and trading statistics.
Get markets
HTTP Request
GET /api/v1/info/markets?market={market}
Get a list of available markets, their configurations, and trading statistics.
To request data for several markets, use the following format:
GET /api/v1/info/markets?market=market1&market2
.
Please note that the margin schedule by market is not covered by this endpoint. For more details on the margin schedule, please refer to the
documentation
.
Market statuses
Status
Description
ACTIVE
Market is active, and all types of orders are permitted.
REDUCE_ONLY
Market is in reduce only mode, and only reduce only orders are allowed.
DELISTED
Market is delisted, and trading is no longer permitted.
PRELISTED
Market is in prelisting stage, and trading not yet available.
DISABLED
Market is completly disabled, and trading is not allowed.
Query Parameters
Parameter
Required
Type
Description
market
no
string[]
List of names of the requested markets.
Response example:
Copy to Clipboard
{
"status"
:
"ok"
,
"data"
:
[
{
"name"
:
"BTC-USD"
,
"assetName"
:
"BTC"
,
"assetPrecision"
:
6
,
"collateralAssetName"
:
"USD"
,
"collateralAssetPrecision"
:
6
,
"active"
:
true
,
"status"
:
"ACTIVE"
,
"marketStats"
:
{
"dailyVolume"
:
"39659164065"
,
"dailyVolumeBase"
:
"39659164065"
,
"dailyPriceChangePercentage"
:
"5.57"
,
"dailyLow"
:
"39512"
,
"dailyHigh"
:
"42122"
,
"lastPrice"
:
"42000"
,
"askPrice"
:
"42005"
,
"bidPrice"
:
"39998"
,
"markPrice"
:
"39950"
,
"indexPrice"
:
"39940"
,
"fundingRate"
:
"0.001"
,
"nextFundingRate"
:
1701563440
,
"openInterest"
:
"1245.2"
,
"openInterestBase"
:
"1245.2"
},
"tradingConfig"
:
{
"minOrderSize"
:
"0.001"
,
"minOrderSizeChange"
:
"0.001"
,
"minPriceChange"
:
"0.001"
,
"maxMarketOrderValue"
:
"1000000"
,
"maxLimitOrderValue"
:
"5000000"
,
"maxPositionValue"
:
"10000000"
,
"maxLeverage"
:
"50"
,
"maxNumOrders"
:
"200"
,
"limitPriceCap"
:
"0.05"
,
"limitPriceFloor"
:
"0.05"
},
"l2Config"
:
{
"type"
:
"STARKX"
,
"collateralId"
:
"0x35596841893e0d17079c27b2d72db1694f26a1932a7429144b439ba0807d29c"
,
"collateralResolution"
:
1000000
,
"syntheticId"
:
"0x4254432d3130000000000000000000"
,
"syntheticResolution"
:
10000000000
}
}
]
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data[].name
yes
string
Name of the market.
data[].assetName
yes
string
Name of the base asset.
data[].assetPrecision
yes
number
Number of decimals for the base asset.
data[].collateralAssetName
yes
string
Name of the collateral asset.
data[].collateralAssetPrecision
yes
number
Number of decimals for the collateral asset.
data[].active
yes
boolean
Indicates if the market is currently active. Can be
true
or
false
.
data[].status
yes
string
Market status.
data[].marketStats.dailyVolume
yes
string
Trading volume of the market in the previous 24 hours in the collateral asset.
data[].marketStats.dailyVolumeBase
yes
string
Trading volume of the market in the previous 24 hours in the base asset.
data[].marketStats.dailyPriceChange
yes
string
Absolute price change of the last trade price over the past 24 hours.
data[].marketStats.dailyPriceChangePercentage
yes
string
Percent price change of the last trade price over the past 24 hours.
data[].marketStats.dailyLow
yes
string
Lowest trade price over the past 24 hours.
data[].marketStats.dailyHigh
yes
string
Highest trade price over the past 24 hours.
data[].marketStats.lastPrice
yes
string
Last price of the market.
data[].marketStats.askPrice
yes
string
Current best ask price of the market.
data[].marketStats.bidPrice
yes
string
Current best bid price of the market.
data[].marketStats.markPrice
yes
string
Current mark price of the market.
data[].marketStats.indexPrice
yes
string
Current index price of the market.
data[].marketStats.fundingRate
yes
string
Current funding rate, calculated every minute.
data[].marketStats.nextFundingRate
yes
number
Timestamp of the next funding update.
data[].marketStats.openInterest
yes
string
Open interest in collateral asset.
data[].marketStats.openInterestBase
yes
string
Open interest in base asset.
data[].tradingConfig.minOrderSize
yes
string
Minimum order size for the market.
data[].tradingConfig.minOrderSizeChange
yes
string
Minimum order size change for the market.
data[].tradingConfig.minPriceChange
yes
string
Minimum price change for the market.
data[].tradingConfig.maxMarketOrderValue
yes
string
Maximum market order value for the market.
data[].tradingConfig.maxLimitOrderValue
yes
string
Maximum limit order value for the market.
data[].tradingConfig.maxPositionValue
yes
string
Maximum position value for the market.
data[].tradingConfig.maxLeverage
yes
string
Maximum leverage available for the market.
data[].tradingConfig.maxNumOrders
yes
string
Maximum number of open orders for the market.
data[].tradingConfig.limitPriceCap
yes
string
Limit order price cap.
data[].tradingConfig.limitPriceFloor
yes
string
Limit order floor ratio.
data[].l2Config.type
yes
string
Type of Layer 2 solution. Currently, only 'STARKX' is supported.
data[].l2Config.collateralId
yes
string
Starknet collateral asset ID.
data[].l2Config.collateralResolution
yes
number
Collateral asset resolution, the number of quantums (Starknet units) that fit within one "human-readable" unit of the collateral asset.
data[].l2Config.syntheticId
yes
string
Starknet synthetic asset ID.
data[].l2Config.syntheticResolution
yes
number
Synthetic asset resolution, the number of quantums (Starknet units) that fit within one "human-readable" unit of the synthetic asset.
Get market statistics
HTTP Request
GET /api/v1/info/markets/{market}/stats
Get the latest trading statistics for an individual market.
Please note that the returned funding rate represents the most recent funding rate, which is calculated every minute.
URL Parameters
Parameter
Required
Type
Description
market
yes
string
Name of the requested market.
Successful response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
{
"dailyVolume"
:
"10283410.122959"
,
"dailyVolumeBase"
:
"3343.1217"
,
"dailyPriceChange"
:
"-26.00"
,
"dailyPriceChangePercentage"
:
"-0.0084"
,
"dailyLow"
:
"3057.98"
,
"dailyHigh"
:
"3133.53"
,
"lastPrice"
:
"3085.70"
,
"askPrice"
:
"3089.05"
,
"bidPrice"
:
"3087.50"
,
"markPrice"
:
"3088.439710293828"
,
"indexPrice"
:
"3089.556987078441"
,
"fundingRate"
:
"-0.000059"
,
"nextFundingRate"
:
1716192000000
,
"openInterest"
:
"35827242.257619"
,
"openInterestBase"
:
"11600.4344"
,
"deleverageLevels"
:
{
"shortPositions"
:
[
{
"level"
:
1
,
"rankingLowerBound"
:
"-1354535.1454"
},
{
"level"
:
2
,
"rankingLowerBound"
:
"-6.3450"
},
{
"level"
:
3
,
"rankingLowerBound"
:
"-0.3419"
},
{
"level"
:
4
,
"rankingLowerBound"
:
"0.0000"
}
],
"longPositions"
:
[
{
"level"
:
1
,
"rankingLowerBound"
:
"-2978.4427"
},
{
"level"
:
2
,
"rankingLowerBound"
:
"0.0000"
},
{
"level"
:
3
,
"rankingLowerBound"
:
"0.0000"
},
{
"level"
:
4
,
"rankingLowerBound"
:
"0.0001"
}
]
}
}
}
Error response example:
Copy to Clipboard
{
"status"
:
"ERROR"
,
"error"
:
{
"code"
:
"NOT_FOUND"
,
"message"
:
"Market not found"
}
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data.dailyVolume
yes
string
Trading volume of the market in the previous 24 hours in the collateral asset.
data.dailyVolumeBase
yes
string
Trading volume of the market in the previous 24 hours in the base asset.
data.dailyPriceChange
yes
string
Absolute price change of the last trade price over the past 24 hours.
data.dailyPriceChangePercentage
yes
string
Percent price change of the last trade price over the past 24 hours.
data.dailyLow
yes
string
Lowest trade price over the past 24 hours.
data.dailyHigh
yes
string
Highest trade price over the past 24 hours.
data.lastPrice
yes
string
Last price of the market.
data.askPrice
yes
string
Current best ask price of the market.
data.bidPrice
yes
string
Current best bid price of the market.
data.markPrice
yes
string
Current mark price of the market.
data.indexPrice
yes
string
Current index price of the market.
data.fundingRate
yes
string
Current funding rate, calculated every minute.
data.nextFundingRate
yes
number
Timestamp of the next funding update.
data.openInterest
yes
string
Open interest in collateral asset.
data.openInterestBase
yes
string
Open interest in base asset.
data.deleverageLevels
yes
enum
Auto Deleveraging (ADL) levels for long and short positions, ranging from level 1 (lowest risk) to level 4 (highest risk) of ADL. For details, please refer to the
documentation
.
Get market order book
HTTP Request
GET /api/v1/info/markets/{market}/orderbook
Get the latest orderbook for an individual market.
URL Parameters
Parameter
Required
Type
Description
market
yes
string
Name of the requested market.
Successful response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
{
"market"
:
"BTC-USD"
,
"bid"
:
[
{
"qty"
:
"0.04852"
,
"price"
:
"61827.7"
},
{
"qty"
:
"0.50274"
,
"price"
:
"61820.5"
}
],
"ask"
:
[
{
"qty"
:
"0.04852"
,
"price"
:
"61840.3"
},
{
"qty"
:
"0.4998"
,
"price"
:
"61864.1"
}
]
}
}
Error response example:
Copy to Clipboard
{
"status"
:
"ERROR"
,
"error"
:
{
"code"
:
"NOT_FOUND"
,
"message"
:
"Market not found"
}
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data.market
yes
string
Market name.
data.bid
yes
object[]
List of bid orders.
data.bid[].qty
yes
string
Qty for the price level.
data.bid[].price
yes
string
Bid price.
data.ask
yes
object[]
List of ask orders.
data.ask[].qty
yes
string
Qty for the price level.
data.ask[].price
yes
string
Ask price.
Get market last trades
HTTP Request
GET /api/v1/info/markets/{market}/trades
Get the latest trade for an individual market.
URL Parameters
Parameter
Required
Type
Description
market
yes
string
Name of the requested market.
Successful response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"i"
:
1844000421446684673
,
"m"
:
"BTC-USD"
,
"S"
:
"SELL"
,
"tT"
:
"TRADE"
,
"T"
:
1728478935001
,
"p"
:
"61998.5"
,
"q"
:
"0.04839"
},
{
"i"
:
1844000955650019328
,
"m"
:
"BTC-USD"
,
"S"
:
"SELL"
,
"tT"
:
"TRADE"
,
"T"
:
1728479062365
,
"p"
:
"61951.4"
,
"q"
:
"0.00029"
}
]
}
Error response example:
Copy to Clipboard
{
"status"
:
"ERROR"
,
"error"
:
{
"code"
:
"NOT_FOUND"
,
"message"
:
"Market not found"
}
}
Response
Parameter
Type
Description
data[].i
number
Trade ID.
data[].m
string
Market name.
data[].S
string
Side of taker trades. Can be
BUY
or
SELL
.
data[].tT
string
Trade type. Can be
TRADE
,
LIQUIDATION
or
DELEVERAGE
.
data[].T
number
Timestamp (in epoch milliseconds) when the trade happened.
data[].p
string
Trade price.
data[].q
string
Trade quantity in base asset.
Get candles history
HTTP Request
GET /api/v1/info/candles/{market}/{candleType}
Get the candles history for an individual market for the timeframe specified in the request. Candles are sorted by timestamp in descending order.
Available price types include:
Trades (last) price:
GET /api/v1/info/candles/{market}/trades
.
Mark price:
GET /api/v1/info/candles/{market}/mark-prices
.
Index price:
GET /api/v1/info/candles/{market}/index-prices
.
The endpoint returns a maximum of 10,000 records.
URL Parameters
Parameter
Required
Type
Description
market
yes
string
Name of the requested market.
candleType
yes
string
Price type. Can be
trades
,
mark-prices
, or
index-prices
.
Query Parameters
Parameter
Required
Type
Description
interval
yes
string
The time interval between data points.
limit
yes
number
The maximum number of items that should be returned.
endTime
no
number
End timestamp (in epoch milliseconds) for the requested period.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"o"
:
"65206.2"
,
"l"
:
"65206.2"
,
"h"
:
"65206.2"
,
"c"
:
"65206.2"
,
"v"
:
"0.0"
,
"T"
:
1715797320000
}
]
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
data[].o
yes
string
Open price.
data[].c
yes
string
Close price.
data[].h
yes
string
Highest price.
data[].l
yes
string
Lowest price.
data[].v
yes
string
Trading volume (Only for
trades
candles).
data[].T
yes
number
Starting timestamp (in epoch milliseconds) for the candle.
Get funding rates history
HTTP Request
GET /api/v1/info/{market}/funding?startTime={startTime}&endTime={endTime}
Get the funding rates history for an individual market for the timeframe specified in the request. The funding rates are sorted by timestamp in descending order.
The endpoint returns a maximum of 10,000 records; pagination should be used to access records beyond this limit.
While the funding rate is calculated every minute, it is only applied once per hour. The records represent the 1-hour rates that were applied for the payment of funding fees.
For details on how the funding rate is calculated on Extended, please refer to the
documentation
.
URL Parameters
Parameter
Required
Type
Description
market
yes
string
Names of the requested market.
Query Parameters
Parameter
Required
Type
Description
startTime
yes
number
Starting timestamp (in epoch milliseconds) for the requested period.
endTime
yes
number
Ending timestamp (in epoch milliseconds) for the requested period.
cursor
no
number
Determines the offset of the returned result. To get the next result page, you can use the cursor from the pagination section of the previous response.
limit
no
number
Maximum number of items that should be returned.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"m"
:
"BTC-USD"
,
"T"
:
1701563440
,
"f"
:
"0.001"
}
],
"pagination"
:
{
"cursor"
:
1784963886257016832
,
"count"
:
1
}
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data[].m
yes
string
Name of the requested market.
data[].T
yes
number
Timestamp (in epoch milliseconds) when the funding rate was calculated and applied.
data[].f
yes
string
Funding rates used for funding fee payments.
Get open interest history
HTTP Request
GET /api/v1/info/{market}/open-interests?interval={interval}&startTime={startTime}&endTime={endTime}
Get the open interest history for an individual market for the timeframe specified in the request. The open interests are sorted by timestamp in descending order.
The endpoint returns a maximum of 300 records; proper combination of start and end time should be used to access records beyond this limit.
URL Parameters
Parameter
Required
Type
Description
market
yes
string
Names of the requested market.
Query Parameters
Parameter
Required
Type
Description
startTime
yes
number
Starting timestamp (in epoch milliseconds) for the requested period.
endTime
yes
number
Ending timestamp (in epoch milliseconds) for the requested period.
interval
yes
enum
P1H for hour and P1D for day
limit
no
number
Maximum number of items that should be returned.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"i"
:
"151193.8952300000000000"
,
"I"
:
"430530.0000000000000000"
,
"t"
:
1749513600000
},
{
"i"
:
"392590.9522500000000000"
,
"I"
:
"1147356.0000000000000000"
,
"t"
:
1749600000000
},
{
"i"
:
"397721.7285100000000000"
,
"I"
:
"1224362.0000000000000000"
,
"t"
:
1749686400000
}
]
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data[].i
yes
string
Open interest in USD.
data[].I
yes
string
Open interest in synthetic asset.
data[].t
yes
number
Timestamp (in epoch milliseconds) when the funding rate was calculated and applied.
Private REST-API
Account
You can create up to ten Extended sub-accounts for each wallet address. For more details, please refer to the
Authentication section
of the API Documentation.
The Private API endpoints listed below grant access to details specific to each sub-account, such as balances, transactions, positions, orders, trades, and the fee rates applied. Additionally, there are endpoints for retrieving the current leverage and adjusting it.
Please note that all endpoints in this section will only return records for the authenticated sub-account.
Get account details
HTTP Request
GET /api/v1/user/account/info
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
{
"status"
:
"ACTIVE"
,
"l2Key"
:
"0x123"
,
"l2Vault"
:
321
,
"accountId"
:
123
,
"description"
:
"abc"
,
"bridgeStarknetAddress"
:
"0x21be84f913dbddbfc0a3993e1f949933139f427f88eb6bfd247ab3ef7174487"
}
}
Get current account details.
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data.status
yes
string
Account status.
data.l2Key
yes
string
Account public key in perp contract.
data.l2Vault
yes
string
Position ID in perp contract.
data.accountId
yes
string
Account ID.
data.description
no
string
Account description (name).
data.bridgeStarknetAddress
yes
string
Starknet account address for EVM bridging.
Get balance
HTTP Request
GET /api/v1/user/balance
Get key balance details for the authenticated sub-account. Returns a 404 error if the userâ€™s balance is 0.
Account Balance = Deposits - Withdrawals + Realised PnL.
Equity = Account Balance + Unrealised PnL.
Available Balance for Trading = Equity - Initial Margin Requirement.
Available Balance for Withdrawals = max(0, Wallet Balance + min(0,Unrealised PnL) - Initial Margin Requirement).
Unrealised PnL (mark-price-based) = The sum of unrealised PnL across open positions, calculated as Position Size * (Mark Price - Entry Price).
Unrealised PnL (mid-price-based) = The sum of unrealised PnL across open positions, calculated as Position Size * (Mid Price - Entry Price).
Initial Margin Requirement for a given market = Max(Abs(Position Value + Value of Buy Orders), Abs(Position Value + Value of Sell Orders))*1/Leverage.
Account Margin Ratio = Maintenance Margin requirement of all open positions / Equity. Liquidation is triggered when Account Margin Ratio > 100%.
Account Exposure = Sum(All positions value)
Account Leverage = Exposure / Equity.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
{
"collateralName"
:
"USDC"
,
"balance"
:
"13500"
,
"equity"
:
"12000"
,
"availableForTrade"
:
"1200"
,
"availableForWithdrawal"
:
"100"
,
"unrealisedPnl"
:
"-10.1"
,
"initialMargin"
:
"160"
,
"marginRatio"
:
"1.5"
,
"exposure"
:
"12751.859629"
,
"leverage"
:
"1275.1860"
,
"updatedTime"
:
1701563440
}
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data.collateralName
yes
string
Name of the collateral asset used for the account.
data.balance
yes
string
Account balance expressed in the collateral asset, also known as Wallet balance.
data.equity
yes
string
Equity of the account.
data.availableForTrade
yes
string
Available Balance for Trading.
data.availableForWithdrawal
yes
string
Available Balance for Withdrawals.
data.unrealisedPnl
yes
string
Current unrealised PnL of the account.
data.initialMargin
yes
string
Collateral used to open the positions and orders.
data.marginRatio
yes
string
Margin ratio of the account.
data.exposure
yes
string
Exposure of the account.
data.leverage
yes
string
Leverage of the account.
data.updatedTime
yes
number
Timestamp (in epoch milliseconds) when the server generated the balance message.
Get deposits, withdrawals, transfers history
HTTP Request
GET /api/v1/user/assetOperations?&type={type}&status={status}
Get the history of deposits, withdrawals, and transfers between sub-accounts for the authenticated sub-account. Optionally, the request can be filtered by a specific transaction type or status.
The endpoint returns 50 records per page; pagination should be used to access records beyond this limit. Transactions are sorted by timestamp in descending order.
Transactions types
Transaction
Description
DEPOSIT
Deposit.
CLAIM
Testing funds claim. Available only on Extended Testnet.
TRANSFER
Transfer between sub-accounts within one wallet.
WITHDRAWAL
Withdrawal.
Transactions statuses
Status
Description
CREATED
Transaction created on Extended.
IN_PROGRESS
Transaction is being processed by Extended, Starknet or bridge provider.
COMPLETED
Transaction completed.
REJECTED
Transaction rejected.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"id"
:
"1951255127004282880"
,
"type"
:
"TRANSFER"
,
"status"
:
"COMPLETED"
,
"amount"
:
"-3.0000000000000000"
,
"fee"
:
"0"
,
"asset"
:
1
,
"time"
:
1754050449502
,
"accountId"
:
100009
,
"counterpartyAccountId"
:
100023
},
{
"id"
:
"0x6795eac4ebbdd9fb88f85e3ce4ce4e61895049591c89ad5db8046a4546d2cdd"
,
"type"
:
"DEPOSIT"
,
"status"
:
"COMPLETED"
,
"amount"
:
"4.9899990000000000"
,
"fee"
:
"0.0000000000000000"
,
"asset"
:
1
,
"time"
:
1753872990528
,
"accountId"
:
100009
,
"transactionHash"
:
"0x93829e61480b528bb18c1b94f0afbc672fb2b340fbfd2f329dffc4180e24b894"
,
"chain"
:
"ETH"
},
{
"id"
:
"1950490023665475584"
,
"type"
:
"WITHDRAWAL"
,
"status"
:
"COMPLETED"
,
"amount"
:
"-4.0000000000000000"
,
"fee"
:
"0.0001000000000000"
,
"asset"
:
1
,
"time"
:
1753868034651
,
"accountId"
:
100009
,
"transactionHash"
:
"0x6d89968d72fc766691d4772048edaf667c88894aedf71f0490c2592c1d268691"
,
"chain"
:
"ETH"
},
],
"pagination"
:
{
"cursor"
:
23
,
"count"
:
23
}
}
Query Parameters
Parameter
Required
Type
Description
type
no
string
Transaction type. Refer to the list of transaction types in the endpoint description above.
status
no
string
Transaction status. Refer to the list of statuses in the endpoint description above.
cursor
no
Determines the offset of the returned result. It represents the ID of the item after which you want to retrieve the next result. To get the next result page, you can use the cursor from the pagination section of the previous response.
limit
no
number
Maximum number of items that should be returned.
Response
Parameter
Required
Type
Description
status
yes
string
Response status. Can be
OK
or
ERROR
.
data[].id
yes
number or string
Transaction ID. A number assigned by Extended for transfers and withdrawals. An onchain id string for deposits.
data[].type
yes
string
Transaction type. Refer to the list of transaction types in the endpoint description above.
data[].status
yes
string
Transaction status. Refer to the list of statuses in the endpoint description above.
data[].amount
yes
string
Transaction amount, absolute value in collateral asset.
data[].fee
yes
string
Fee paid.
data[].asset
yes
string
Collateral asset name.
data[].time
yes
number
Timestamp (epoch milliseconds) when the transaction was updated.
data[].accountId
yes
number
Account ID; source account for transfers and withdrawals; destination account for deposits.
data[].counterpartyAccountId
no
number
Account ID; destination account for transfers.
data[].transactionHash
no
string
Onchain transaction hash. Not available for transfers.
data[].chain
no
string
Source chain name for deposits; target chain name for withdrawals.
Get positions
HTTP Request
GET /api/v1/user/positions?market={market}&side={side}
Get all open positions for the authenticated sub-account. Optionally, the request can be filtered by a specific market or position side (
long
or
short
).
To request data for multiple markets, use the following format:
GET /api/v1/user/positions?market=market1&market2
.
Query Parameters
Parameter
Required
Type
Description
market
no
string
List of names of the requested markets.
side
no
string
Position side. Can be
LONG
or
SHORT
.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"id"
:
1
,
"accountId"
:
1
,
"market"
:
"BTC-USD"
,
"side"
:
"LONG"
,
"leverage"
:
"10"
,
"size"
:
"0.1"
,
"value"
:
"4000"
,
"openPrice"
:
"39000"
,
"markPrice"
:
"40000"
,
"liquidationPrice"
:
"38200"
,
"margin"
:
"20"
,
"unrealisedPnl"
:
"1000"
,
"realisedPnl"
:
"1.2"
,
"tpTriggerPrice"
:
"41000"
,
"tpLimitPrice"
:
"41500"
,
"slTriggerPrice"
:
"39500"
,
"slLimitPrice"
:
"39000"
,
"adl"
:
"2.5"
,
"maxPositionSize"
:
"0.2"
,
"createdTime"
:
1701563440000
,
"updatedTime"
:
1701563440
}
]
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data[].id
yes
number
Position ID assigned by Extended.
data[].accountId
yes
number
Account ID.
data[].market
yes
string
Market name.
data[].side
yes
string
Position side. Can be
LONG
or
SHORT
.
data[].leverage
yes
string
Position leverage.
data[].size
yes
string
Position size, absolute value in base asset.
data[].value
yes
string
Position value, absolute value in collateral asset.
data[].openPrice
yes
string
Position's open (entry) price.
data[].markPrice
yes
string
Current mark price of the market.
data[].liquidationPrice
yes
string
Position's liquidation price.
data[].margin
yes
string
Position's margin in collateral asset.
data[].unrealisedPnl
yes
string
Position's Unrealised PnL.
data[].realisedPnl
yes
string
Position's Realised PnL.
data[].tpTriggerPrice
no
string
Take Profit Trigger price.
data[].tpLimitPrice
no
string
Take Profit Limit price.
data[].slTriggerPrice
no
string
Stop Loss Trigger price.
data[].slLimitPrice
no
string
Stop Loss Limit price.
data[].maxPositionSize
yes
string
Maximum allowed position size, absolute value in base asset.
data[].adl
yes
string
Position's Auto-Deleveraging (ADL) ranking in the queue, expressed as a percentile. A value closer to 100 indicates a higher likelihood of being ADLed.
data[].createdTime
yes
number
Timestamp (epoch milliseconds) when the position was created.
data[].updatedTime
yes
number
Timestamp (epoch milliseconds) when the position was updated.
Get positions history
HTTP Request
GET /api/v1/user/positions/history?market={market}&side={side}
Get all open and closed positions for the authenticated sub-account. Optionally, the request can be filtered by a specific market or position side (
long
or
short
).
To request data for several markets, use the following format: GET /api/v1/user/positions/history?market=market1&market2.
The endpoint returns a maximum of 10,000 records; pagination should be used to access records beyond this limit.
Query Parameters
Parameter
Required
Type
Description
market
no
string
List of names of the requested markets.
side
no
string
Position side. Can be
long
or
short
.
cursor
no
number
Determines the offset of the returned result. It represents the ID of the item after which you want to retrieve the next result. To get the next result page, you can use the cursor from the pagination section of the previous response.
limit
no
number
Maximum number of items that should be returned.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"id"
:
1784963886257016832
,
"accountId"
:
1
,
"market"
:
"BTC-USD"
,
"side"
:
"LONG"
,
"exitType"
:
"TRADE"
,
"leverage"
:
"10"
,
"size"
:
"0.1"
,
"maxPositionSize"
:
"0.2"
,
"openPrice"
:
"39000"
,
"exitPrice"
:
"40000"
,
"realisedPnl"
:
"1.2"
,
"createdTime"
:
1701563440000
,
"closedTime"
:
1701563440
}
],
"pagination"
:
{
"cursor"
:
1784963886257016832
,
"count"
:
1
}
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data[].id
yes
number
Position ID assigned by Extended.
data[].accountId
yes
number
Account ID.
data[].market
yes
string
Market name.
data[].side
yes
string
Position side. Can be
LONG
or
SHORT
.
data[].exitType
no
string
The exit type of the last trade that reduced the position. Can be
TRADE
,
LIQUIDATION
, or
DELEVERAGE
.
data[].leverage
yes
string
Position leverage.
data[].size
yes
string
Position size, absolute value in base asset.
data[].maxPositionSize
yes
string
Maximum position size during the position's lifetime, absolute value in base asset.
data[].openPrice
yes
string
The weighted average price of trades that contributed to increasing the position.
data[].exitPrice
no
string
The weighted average price of trades that contributed to decreasing the position.
data[].realisedPnl
yes
string
Position Realised PnL.
data[].createdTime
yes
number
Timestamp (in epoch milliseconds) when the position was created.
data[].closedTime
no
number
Timestamp (in epoch milliseconds) when the position was closed, applicable only for closed positions.
Get open orders
HTTP Request
GET /api/v1/user/orders?market={market}&type={type}&side={side}
Get all open orders for the authenticated sub-account. Optionally, the request can be filtered by a specific market or order type (
limit
,
conditional
,
tpsl
or
twap
).
Open orders correspond to the following order statuses from the list below:
new
,
partially filled
,
untriggered
.
To request data for several markets, use the following format:
GET /api/v1/user/orders?market=market1&market2
.
Order statuses
Status
Description
NEW
Order in the order book, unfilled.
PARTIALLY_FILLED
Order in the order book, partially filled.
FILLED
Order fully filled.
UNTRIGGERED
Conditional order waiting for the trigger price.
CANCELLED
Order cancelled.
REJECTED
Order rejected.
EXPIRED
Order expired.
TRIGGERED
Technical status, transition from
UNTRIGGERED
to
NEW
.
Order status reasons (when cancelled or rejected)
Reason
Description
NONE
Order was accepted.
UNKNOWN
Technical status reason.
UNKNOWN_MARKET
Market does not exist.
DISABLED_MARKET
Market is not active.
NOT_ENOUGH_FUNDS
Insufficient balance to create order.
NO_LIQUIDITY
Not enough liquidity in the market to execute the order.
INVALID_FEE
Fee specified in the create order request is invalid.
INVALID_QTY
Quantity specified is invalid.
INVALID_PRICE
Price specified is invalid.
INVALID_VALUE
Order exceeds the maximum value.
UNKNOWN_ACCOUNT
Account does not exist.
SELF_TRADE_PROTECTION
Order cancelled to prevent self-trading.
POST_ONLY_FAILED
Order could not be posted as a post-only order.
REDUCE_ONLY_FAILED
Reduce-only order failed due to position size conflict.
INVALID_EXPIRE_TIME
Expiration time specified is invalid.
POSITION_TPSL_CONFLICT
TPSL order for the entire position already exists.
INVALID_LEVERAGE
Leverage specified is invalid.
PREV_ORDER_NOT_FOUND
The order to be replaced does not exist.
PREV_ORDER_TRIGGERED
The order to be replaced has been triggered and cannot be replaced.
TPSL_OTHER_SIDE_FILLED
The opposite side of a TP/SL order has been filled.
PREV_ORDER_CONFLICT
Conflict with an existing order during replacement.
ORDER_REPLACED
Order has been replaced by another order.
POST_ONLY_MODE
Exchange is in post-only mode, only post-only orders are allowed.
REDUCE_ONLY_MODE
Exchange is in reduce-only mode, only reduce-only orders are allowed.
TRADING_OFF_MODE
Trading is currently disabled.
NEGATIVE_EQUITY
Account has negative equity.
ACCOUNT_LIQUIDATION
Account is under liquidation.
Query Parameters
Parameter
Required
Type
Description
market
no
string
List of names of the requested markets.
type
no
string
Order type. Can be
LIMIT
,
CONDITIONAL
,
TPSL
or
TWAP
.
side
no
string
Order side. Can be
BUY
or
SELL
.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"id"
:
1775511783722512384
,
"accountId"
:
3017
,
"externalId"
:
"2554612759479898620327573136214120486511160383028978112799136270841501275076"
,
"market"
:
"ETH-USD"
,
"type"
:
"LIMIT"
,
"side"
:
"BUY"
,
"status"
:
"PARTIALLY_FILLED"
,
"price"
:
"3300"
,
"averagePrice"
:
"3297.00"
,
"qty"
:
"0.2"
,
"filledQty"
:
"0.1"
,
"payedFee"
:
"0.0120000000000000"
,
"trigger"
:
{
"triggerPrice"
:
"3300"
,
"triggerPriceType"
:
"LAST"
,
"triggerPriceDirection"
:
"UP"
,
"executionPriceType"
:
"MARKET"
},
"takeProfit"
:
{
"triggerPrice"
:
"3500"
,
"triggerPriceType"
:
"LAST"
,
"price"
:
"3340"
,
"priceType"
:
"MARKET"
},
"stopLoss"
:
{
"triggerPrice"
:
"2800"
,
"triggerPriceType"
:
"LAST"
,
"price"
:
"2660"
,
"priceType"
:
"MARKET"
},
"reduceOnly"
:
false
,
"postOnly"
:
false
,
"createdTime"
:
1701563440000
,
"updatedTime"
:
1701563440000
,
"timeInForce"
:
"IOC"
,
"expireTime"
:
1712754771819
}
]
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data[].id
yes
number
Order ID assigned by Extended.
data[].externalId
yes
string
Order ID assigned by user.
data[].accountId
yes
number
Account ID.
data[].market
yes
string
Market name.
data[].status
yes
string
Order status.
data[].statusReason
no
string
Reason for
REJECTED
or
CANCELLED
status.
data[].type
yes
string
Order type. Can be
LIMIT
,
CONDITIONAL
,
TPSL
or
TWAP
.
data[].side
yes
string
Order side. Can be
BUY
or
SELL
.
data[].price
no
string
Worst accepted price in the collateral asset.
data[].averagePrice
no
string
Actual filled price, empty if not filled.
data[].qty
yes
string
Order size in base asset.
data[].filledQty
no
string
Actual filled quantity in base asset.
data[].payedFee
no
string
Paid fee.
data[].reduceOnly
no
boolean
Whether the order is Reduce-only.
data[].postOnly
no
boolean
Whether the order is Post-only.
data[].trigger.triggerPrice
no
string
Trigger price for conditional orders.
data[].trigger.triggerPriceType
no
string
Trigger price type. Can be
LAST
,
MARK
or
INDEX
.
data[].trigger.triggerPriceDirection
no
string
Indicates whether the order should be triggered when the price is above or below the set trigger price. It can be
UP
(the order will be triggered when the price reaches or surpasses the set trigger price) or
DOWN
(the order will be triggered when the price reaches or drops below the set trigger price).
data[].trigger.executionPriceType
no
string
Execution price type. Can be
LIMIT
or
MARKET
.
data[].tpSlType
no
string
TPSL type determining TPSL order size. Can be
ORDER
or
POSITION
.
data[].takeProfit.triggerPrice
no
string
Take Profit Trigger price.
data[].takeProfit.triggerPriceType
no
string
Take Profit Trigger price type. Can be
LAST
,
MARK
or
INDEX
.
data[].takeProfit.price
no
string
Take Profit order price.
data[].takeProfit.priceType
no
string
Indicates whether the Take profit order should be executed as
MARKET
or
LIMIT
order.
data[].stopLoss.triggerPrice
no
string
Stop loss Trigger price.
data[].stopLoss.triggerPriceType
no
string
Stop Loss Trigger price type. Can be
LAST
,
MARK
or
INDEX
.
data[].stopLoss.price
no
string
Stop loss order price.
data[].stopLoss.priceType
no
string
Indicates whether the Stop loss order should be executed as
MARKET
or
LIMIT
order.
data[].createdTime
yes
number
Timestamp (in epoch milliseconds) of order creation.
data[].updatedTime
yes
number
Timestamp (in epoch milliseconds) of order update.
data[].timeInForce
yes
string
Time-in-force. Can be
GTT
(Good till time) or
IOC
(Immediate or cancel).
data[].expireTime
yes
number
Timestamp (in epoch milliseconds) when the order expires.
Get orders history
HTTP Request
GET /api/v1/user/orders/history?market={market}&type={type}&side={side}&id={id}&externalId={externalId}
Get orders history for the authenticated sub-account. Optionally, the request can be filtered by a specific market or order type (
limit
,
market
,
conditional
,
tpsl
or
twap
). Note: Scaled orders are represented as multiple individual
limit
orders in the system.
Orders history corresponds to the following order statuses from the list below:
filled
,
cancelled
,
rejected
,
expired
.
To request data for several markets, use the following format:
GET /api/v1/user/orders/history?market=market1&market2
.
The endpoint returns a maximum of 10,000 records; pagination should be used to access records beyond this limit. The records for closed non-filled orders are available only for the past 7 days.
Order statuses
Status
Description
NEW
Order in the order book, unfilled.
PARTIALLY_FILLED
Order in the order book, partially filled.
FILLED
Order fully filled.
UNTRIGGERED
Conditional order waiting for the trigger price.
CANCELLED
Order cancelled.
REJECTED
Order rejected.
EXPIRED
Order expired.
TRIGGERED
Technical status, transition from
UNTRIGGERED
to
NEW
.
Query Parameters
Parameter
Required
Type
Description
id
no
number
List of internal Ids of the requested orders.
externalId
no
string[]
List of external Ids of the requested orders.
market
no
string[]
List of names of the requested markets.
type
no
string
Order type. Can be
limit
,
market
,
conditional
,
tpsl
or
twap
.
side
no
string
Order side. Can be
buy
or
sell
.
cursor
no
number
Determines the offset of the returned result. It represents the ID of the item after which you want to retrieve the next result. To get the next result page, you can use the cursor from the pagination section of the previous response.
limit
no
number
Maximum number of items that should be returned.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"id"
:
1784963886257016832
,
"externalId"
:
"ExtId-1"
,
"accountId"
:
1
,
"market"
:
"BTC-USD"
,
"status"
:
"FILLED"
,
"type"
:
"LIMIT"
,
"side"
:
"BUY"
,
"price"
:
"39000"
,
"averagePrice"
:
"39000"
,
"qty"
:
"0.2"
,
"filledQty"
:
"0.1"
,
"payedFee"
:
"0.0120000000000000"
,
"reduceOnly"
:
false
,
"postOnly"
:
false
,
"trigger"
:
{
"triggerPrice"
:
"34000"
,
"triggerPriceType"
:
"LAST"
,
"triggerPriceDirection"
:
"UP"
,
"executionPriceType"
:
"MARKET"
},
"tpslType"
:
"ORDER"
,
"takeProfit"
:
{
"triggerPrice"
:
"34000"
,
"triggerPriceType"
:
"LAST"
,
"price"
:
"35000"
,
"priceType"
:
"MARKET"
,
"starkExSignature"
:
""
},
"stopLoss"
:
{
"triggerPrice"
:
"34000"
,
"triggerPriceType"
:
"LAST"
,
"price"
:
"35000"
,
"priceType"
:
"MARKET"
,
"starkExSignature"
:
""
},
"createdTime"
:
1701563440000
,
"updatedTime"
:
1701563440000
,
"timeInForce"
:
"IOC"
,
"expireTime"
:
1706563440
}
],
"pagination"
:
{
"cursor"
:
1784963886257016832
,
"count"
:
1
}
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data[].id
yes
number
Order ID assigned by Extended.
data[].externalId
yes
string
Order ID assigned by user.
data[].accountId
yes
number
Account ID.
data[].market
yes
string
Market name.
data[].status
yes
string
Order status.
data[].statusReason
no
string
Reason for
REJECTED
or
CANCELLED
status.
data[].type
yes
string
Order type. Can be
LIMIT
,
MARKET
,
CONDITIONAL
,
TPSL
or
TWAP
.
data[].side
yes
string
Order side. Can be
BUY
or
SELL
.
data[].price
no
string
Worst accepted price in the collateral asset.
data[].averagePrice
no
string
Actual filled price, empty if not filled.
data[].qty
yes
string
Order size in base asset.
data[].filledQty
no
string
Actual filled quantity in base asset.
data[].payedFee
no
string
Paid fee.
data[].reduceOnly
no
boolean
Whether the order is Reduce-only.
data[].postOnly
no
boolean
Whether the order is Post-only.
data[].trigger.triggerPrice
no
string
Trigger price for conditional orders.
data[].trigger.triggerPriceType
no
string
Trigger price type . Can be
LAST
,
MARK
or
INDEX
.
data[].trigger.triggerPriceDirection
no
string
Indicates whether the order should be triggered when the price is above or below the set trigger price. It can be
UP
(the order will be triggered when the price reaches or surpasses the set trigger price) or
DOWN
(the order will be triggered when the price reaches or drops below the set trigger price).
data[].trigger.executionPriceType
no
string
Execution price type. Can be
LIMIT
or
MARKET
.
data[].tpSlType
no
string
TPSL type determining TPSL order size. Can be
ORDER
or
POSITION
.
data[].takeProfit.triggerPrice
no
string
Take Profit Trigger price.
data[].takeProfit.triggerPriceType
no
string
Take Profit Trigger price type. Can be
LAST
,
MARK
or
INDEX
.
data[].takeProfit.price
no
string
Take Profit order price.
data[].takeProfit.priceType
no
string
Indicates whether the Take profit order should be executed as
MARKET
or
LIMIT
order.
data[].stopLoss.triggerPrice
no
string
Stop loss Trigger price.
data[].stopLoss.triggerPriceType
no
string
Stop Loss Trigger price type. Can be
LAST
,
MARK
or
INDEX
.
data[].stopLoss.price
no
string
Stop loss order price.
data[].stopLoss.priceType
no
string
Indicates whether the Stop loss order should be executed as
MARKET
or
LIMIT
order.
data[].createdTime
yes
number
Timestamp (in epoch milliseconds) of order creation.
data[].updatedTime
yes
number
Timestamp (in epoch milliseconds) of order update.
data[].timeInForce
yes
string
Time-in-force. Can be
GTT
(Good till time) or
IOC
(Immediate or cancel).
data[].expireTime
yes
number
Timestamp (in epoch milliseconds) when the order expires.
Get order by id
HTTP Request
GET /api/v1/user/orders/{id}
Get order by id for the authenticated sub-account.
Order statuses
Status
Description
NEW
Order in the order book, unfilled.
PARTIALLY_FILLED
Order in the order book, partially filled.
FILLED
Order fully filled.
UNTRIGGERED
Conditional order waiting for the trigger price.
CANCELLED
Order cancelled.
REJECTED
Order rejected.
EXPIRED
Order expired.
TRIGGERED
Technical status, transition from
UNTRIGGERED
to
NEW
.
URL Parameters
Parameter
Required
Type
Description
id
yes
number
Order to be retrieved, ID assigned by Extended.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
{
"id"
:
1784963886257016832
,
"externalId"
:
"ExtId-1"
,
"accountId"
:
1
,
"market"
:
"BTC-USD"
,
"status"
:
"FILLED"
,
"type"
:
"LIMIT"
,
"side"
:
"BUY"
,
"price"
:
"39000"
,
"averagePrice"
:
"39000"
,
"qty"
:
"0.2"
,
"filledQty"
:
"0.1"
,
"payedFee"
:
"0.0120000000000000"
,
"reduceOnly"
:
false
,
"postOnly"
:
false
,
"trigger"
:
{
"triggerPrice"
:
"34000"
,
"triggerPriceType"
:
"LAST"
,
"triggerPriceDirection"
:
"UP"
,
"executionPriceType"
:
"MARKET"
},
"tpslType"
:
"ORDER"
,
"takeProfit"
:
{
"triggerPrice"
:
"34000"
,
"triggerPriceType"
:
"LAST"
,
"price"
:
"35000"
,
"priceType"
:
"MARKET"
,
"starkExSignature"
:
""
},
"stopLoss"
:
{
"triggerPrice"
:
"34000"
,
"triggerPriceType"
:
"LAST"
,
"price"
:
"35000"
,
"priceType"
:
"MARKET"
,
"starkExSignature"
:
""
},
"createdTime"
:
1701563440000
,
"updatedTime"
:
1701563440000
,
"timeInForce"
:
"IOC"
,
"expireTime"
:
1706563440
}
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data.id
yes
number
Order ID assigned by Extended.
data.externalId
yes
string
Order ID assigned by user.
data.accountId
yes
number
Account ID.
data.market
yes
string
Market name.
data.status
yes
string
Order status.
data.statusReason
no
string
Reason for
REJECTED
or
CANCELLED
status.
data.type
yes
string
Order type. Can be
LIMIT
,
MARKET
,
CONDITIONAL
,
TPSL
or
TWAP
.
data.side
yes
string
Order side. Can be
BUY
or
SELL
.
data.price
no
string
Worst accepted price in the collateral asset.
data.averagePrice
no
string
Actual filled price, empty if not filled.
data.qty
yes
string
Order size in base asset.
data.filledQty
no
string
Actual filled quantity in base asset.
data.payedFee
no
string
Paid fee.
data.reduceOnly
no
boolean
Whether the order is Reduce-only.
data.postOnly
no
boolean
Whether the order is Post-only.
data.trigger.triggerPrice
no
string
Trigger price for conditional orders.
data.trigger.triggerPriceType
no
string
Trigger price type . Can be
LAST
,
MARK
or
INDEX
.
data.trigger.triggerPriceDirection
no
string
Indicates whether the order should be triggered when the price is above or below the set trigger price. It can be
UP
(the order will be triggered when the price reaches or surpasses the set trigger price) or
DOWN
(the order will be triggered when the price reaches or drops below the set trigger price).
data.trigger.executionPriceType
no
string
Execution price type. Can be
LIMIT
or
MARKET
.
data.tpSlType
no
string
TPSL type determining TPSL order size. Can be
ORDER
or
POSITION
.
data.takeProfit.triggerPrice
no
string
Take Profit Trigger price.
data.takeProfit.triggerPriceType
no
string
Take Profit Trigger price type. Can be
LAST
,
MARK
or
INDEX
.
data.takeProfit.price
no
string
Take Profit order price.
data.takeProfit.priceType
no
string
Indicates whether the Take profit order should be executed as
MARKET
or
LIMIT
order.
data.stopLoss.triggerPrice
no
string
Stop loss Trigger price.
data.stopLoss.triggerPriceType
no
string
Stop Loss Trigger price type. Can be
LAST
,
MARK
or
INDEX
.
data.stopLoss.price
no
string
Stop loss order price.
data.stopLoss.priceType
no
string
Indicates whether the Stop loss order should be executed as
MARKET
or
LIMIT
order.
data.createdTime
yes
number
Timestamp (in epoch milliseconds) of order creation.
data.updatedTime
yes
number
Timestamp (in epoch milliseconds) of order update.
data.timeInForce
yes
string
Time-in-force. Can be
GTT
(Good till time) or
IOC
(Immediate or cancel).
data.expireTime
yes
number
Timestamp (in epoch milliseconds) when the order expires.
Get orders by external id
HTTP Request
GET /api/v1/user/orders/external/{externalId}
Get orders by external id for the authenticated sub-account.
Order statuses
Status
Description
NEW
Order in the order book, unfilled.
PARTIALLY_FILLED
Order in the order book, partially filled.
FILLED
Order fully filled.
UNTRIGGERED
Conditional order waiting for the trigger price.
CANCELLED
Order cancelled.
REJECTED
Order rejected.
EXPIRED
Order expired.
TRIGGERED
Technical status, transition from
UNTRIGGERED
to
NEW
.
URL Parameters
Parameter
Required
Type
Description
externalId
yes
number
Order to be retrieved, ID assigned by user.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"id"
:
1784963886257016832
,
"externalId"
:
"ExtId-1"
,
"accountId"
:
1
,
"market"
:
"BTC-USD"
,
"status"
:
"FILLED"
,
"type"
:
"LIMIT"
,
"side"
:
"BUY"
,
"price"
:
"39000"
,
"averagePrice"
:
"39000"
,
"qty"
:
"0.2"
,
"filledQty"
:
"0.1"
,
"payedFee"
:
"0.0120000000000000"
,
"reduceOnly"
:
false
,
"postOnly"
:
false
,
"trigger"
:
{
"triggerPrice"
:
"34000"
,
"triggerPriceType"
:
"LAST"
,
"triggerPriceDirection"
:
"UP"
,
"executionPriceType"
:
"MARKET"
},
"tpslType"
:
"ORDER"
,
"takeProfit"
:
{
"triggerPrice"
:
"34000"
,
"triggerPriceType"
:
"LAST"
,
"price"
:
"35000"
,
"priceType"
:
"MARKET"
,
"starkExSignature"
:
""
},
"stopLoss"
:
{
"triggerPrice"
:
"34000"
,
"triggerPriceType"
:
"LAST"
,
"price"
:
"35000"
,
"priceType"
:
"MARKET"
,
"starkExSignature"
:
""
},
"createdTime"
:
1701563440000
,
"updatedTime"
:
1701563440000
,
"timeInForce"
:
"IOC"
,
"expireTime"
:
1706563440
}
]
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data[].id
yes
number
Order ID assigned by Extended.
data[].externalId
yes
string
Order ID assigned by user.
data[].accountId
yes
number
Account ID.
data[].market
yes
string
Market name.
data[].status
yes
string
Order status.
data[].statusReason
no
string
Reason for
REJECTED
or
CANCELLED
status.
data[].type
yes
string
Order type. Can be
LIMIT
,
MARKET
,
CONDITIONAL
,
TPSL
or
TWAP
.
data[].side
yes
string
Order side. Can be
BUY
or
SELL
.
data[].price
no
string
Worst accepted price in the collateral asset.
data[].averagePrice
no
string
Actual filled price, empty if not filled.
data[].qty
yes
string
Order size in base asset.
data[].filledQty
no
string
Actual filled quantity in base asset.
data[].payedFee
no
string
Paid fee.
data[].reduceOnly
no
boolean
Whether the order is Reduce-only.
data[].postOnly
no
boolean
Whether the order is Post-only.
data[].trigger.triggerPrice
no
string
Trigger price for conditional orders.
data[].trigger.triggerPriceType
no
string
Trigger price type . Can be
LAST
,
MARK
or
INDEX
.
data[].trigger.triggerPriceDirection
no
string
Indicates whether the order should be triggered when the price is above or below the set trigger price. It can be
UP
(the order will be triggered when the price reaches or surpasses the set trigger price) or
DOWN
(the order will be triggered when the price reaches or drops below the set trigger price).
data[].trigger.executionPriceType
no
string
Execution price type. Can be
LIMIT
or
MARKET
.
data[].tpSlType
no
string
TPSL type determining TPSL order size. Can be
ORDER
or
POSITION
.
data[].takeProfit.triggerPrice
no
string
Take Profit Trigger price.
data[].takeProfit.triggerPriceType
no
string
Take Profit Trigger price type. Can be
LAST
,
MARK
or
INDEX
.
data[].takeProfit.price
no
string
Take Profit order price.
data[].takeProfit.priceType
no
string
Indicates whether the Take profit order should be executed as
MARKET
or
LIMIT
order.
data[].stopLoss.triggerPrice
no
string
Stop loss Trigger price.
data[].stopLoss.triggerPriceType
no
string
Stop Loss Trigger price type. Can be
LAST
,
MARK
or
INDEX
.
data[].stopLoss.price
no
string
Stop loss order price.
data[].stopLoss.priceType
no
string
Indicates whether the Stop loss order should be executed as
MARKET
or
LIMIT
order.
data[].createdTime
yes
number
Timestamp (in epoch milliseconds) of order creation.
data[].updatedTime
yes
number
Timestamp (in epoch milliseconds) of order update.
data[].timeInForce
yes
string
Time-in-force. Can be
GTT
(Good till time) or
IOC
(Immediate or cancel).
data[].expireTime
yes
number
Timestamp (in epoch milliseconds) when the order expires.
Get trades
HTTP Request
GET /api/v1/user/trades?market={market}&type={type}&side={side}
Get trades history for the authenticated sub-account. Optionally, the request can be filtered by a specific market, by trade type (
trade
,
liquidation
or
deleverage
) and side (
buy
or
sell
).
To request data for several markets, use the following format:
GET /api/v1/user/trades?market=market1&market2
.
The endpoint returns a maximum of 10,000 records; pagination should be used to access records beyond this limit.
Query Parameters
Parameter
Required
Type
Description
market
no
string
List of names of the requested markets.
type
no
string
Trade type. Can be
trade
,
liquidation
or
deleverage
.
side
no
string
Order side. Can be
buy
or
sell
.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"id"
:
1784963886257016832
,
"accountId"
:
3017
,
"market"
:
"BTC-USD"
,
"orderId"
:
9223372036854775808
,
"externalId"
:
"ext-1"
,
"side"
:
"BUY"
,
"price"
:
"58853.4000000000000000"
,
"qty"
:
"0.0900000000000000"
,
"value"
:
"5296.8060000000000000"
,
"fee"
:
"0.0000000000000000"
,
"tradeType"
:
"DELEVERAGE"
,
"createdTime"
:
1701563440000
,
"isTaker"
:
true
}
],
"pagination"
:
{
"cursor"
:
1784963886257016832
,
"count"
:
1
}
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data[].id
yes
number
Trade ID assigned by Extended.
data[].accountId
yes
number
Account ID.
data[].market
yes
string
Market name.
data[].orderId
yes
string
Order ID assigned by Extended.
data[].externalOrderId
yes
string
Order ID assigned by user. Populated only on websocket stream.
data[].side
yes
string
Order side. Can be
BUY
or
SELL
.
data[].averagePrice
yes
string
Actual filled price.
data[].filledQty
yes
string
Actual filled quantity in base asset.
data[].value
yes
string
Actual filled absolute nominal value in collateral asset.
data[].fee
yes
string
Paid fee.
data[].isTaker
yes
boolean
Whether the trade was executed as a taker.
data[].tradeType
yes
string
Trade type. Can be
TRADE
(for regular trades),
LIQUIDATION
(for liquidaton trades) or
DELEVERAGE
(for ADL trades).
data[].createdTime
yes
number
Timestamp (in epoch milliseconds) when the trade happened.
Get funding payments
HTTP Request
GET /api/v1/user/funding/history?market={market}&side={side}&fromTime={fromTime}
Get funding payments history for the authenticated sub-account. Optionally, the request can be filtered by a specific market, by side (
long
or
short
) and from time as a start point.
To request data for several markets, use the following format:
GET /api/v1/user/funding/history?market=market1&market2
.
The endpoint returns a maximum of 10,000 records; pagination should be used to access records beyond this limit.
Query Parameters
Parameter
Required
Type
Description
market
no
string
List of names of the requested markets.
side
no
string
Position side. Can be
long
or
short
.
fromTime
yes
number
Starting timestamp (in epoch milliseconds).
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"id"
:
8341
,
"accountId"
:
3137
,
"market"
:
"BNB-USD"
,
"positionId"
:
1821237954501148672
,
"side"
:
"LONG"
,
"size"
:
"1.116"
,
"value"
:
"560.77401888"
,
"markPrice"
:
"502.48568"
,
"fundingFee"
:
"0"
,
"fundingRate"
:
"0"
,
"paidTime"
:
1723147241346
}
],
"pagination"
:
{
"cursor"
:
8341
,
"count"
:
1
}
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data[].id
yes
number
Funding payment ID assigned by Extended.
data[].accountId
yes
number
Account ID.
data[].market
yes
string
Market name.
data[].positionId
yes
number
Position ID assigned by Extended.
data[].side
yes
string
Position side. Can be
LONG
or
SHORT
.
data[].value
yes
string
Position value at funding payment time.
data[].markPrice
yes
string
Mark price at funding payment time
data[].fundingFee
yes
string
Funding payment size.
data[].fundingRate
yes
string
Funding rate.
data[].paidTime
yes
number
Timestamp (in epoch milliseconds) when the funding payment happened.
Get rebates
HTTP Request
GET /api/v1/user/rebates/stats
Get rebates related data for the authenticated sub-account.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"totalPaid"
:
"0"
,
"rebatesRate"
:
"0"
,
"marketShare"
:
"0.002"
,
"nextTierMakerShare"
:
"0.01"
,
"nextTierRebateRate"
:
"0.00005"
}
]
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data.totalPaid
yes
string
Total rebates paid.
data.rebatesRate
yes
string
Current rebates rate.
data.marketShare
yes
string
Maker volume share.
data.nextTierMakerShare
yes
string
Maker volume share required to increase rebates.
data.nextTierRebateRate
yes
string
Rebates rate for next maker share threshold.
Get current leverage
HTTP Request
GET /api/v1/user/leverage?market={market}
Get current leverage for the authenticated sub-account. You can get current leverage for all markets, a single market, or multiple specific markets.
To request data for several markets, use the format
GET/api/v1/user/leverage?market=market1&market=market2
.
Query Parameters
Parameter
Required
Type
Description
market
no
string
Name of the requested market.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"market"
:
"SOL-USD"
,
"leverage"
:
"10"
}
]
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data.market
yes
string
Market name.
data.leverage
yes
string
Current leverage.
Update leverage
HTTP Request
PATCH /api/v1/user/leverage
Update leverage for an individual market.
Modifying your leverage will impact your
Available balance
and
Initial Margin requirements
of your open position and orders in the market.
To adjust your leverage, you must meet two requirements:
The total value of your open position and triggered orders must remain below the maximum position value allowed for the selected leverage.
Your Available balance must be sufficient to cover the additional Margin requirements (if any) associated with the new leverage.
Failure to meet either of these criteria will result in an error.
For details on Margin requirements, please refer to the
documentation
.
Request example:
Copy to Clipboard
{
"market"
:
"BTC-USD"
,
"leverage"
:
"10"
}
Body Parameters
Parameter
Required
Type
Description
market
yes
string
Name of the requested market.
leverage
yes
string
Target leverage.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
{
"market"
:
"BTC-USD"
,
"leverage"
:
"10"
}
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data.market
yes
string
Market name.
data.leverage
yes
string
Updated leverage.
Get fees
HTTP Request
GET /api/v1/user/fees?market={market}
Get current fees for the sub-account. Currently, Extended features a flat fee structure:
Taker: 0.025%
Maker: 0.000%
The team reserves the right to update the fee schedule going forward.
For updates on the Fee Schedule, please refer to the
documentation
.
Query Parameters
Parameter
Required
Type
Description
market
no
string
Name of the requested market.
builderId
no
string
builder client id
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
[
{
"market"
:
"BTC-USD"
,
"makerFeeRate"
:
"0.00000"
,
"takerFeeRate"
:
"0.00025"
,
"builderFeeRate"
:
"0.0001"
}
]
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data.market
yes
string
Market name.
data.makerFeeRate
yes
string
Maker fee rate.
data.takerFeeRate
yes
string
Taker fee rate.
data.builderFeeRate
yes
string
Builder fee rate.
Order management
The Private API endpoints listed below allow you to create, cancel, and manage orders from the authenticated sub-account.
Starknet-Specific Logic
Extended settles all transactions on-chain on Starknet. As a result, order creation might differ from centralized exchanges in a few ways:
Stark Key Signature: Required for all order management endpoints. For details, please refer to the reference implementation in the
Python SDK
.
Price Parameter: All orders, including market orders, require a price as a mandatory parameter.
Fee Parameter: All orders require a fee as a mandatory parameter. The
Fee
parameter represents the maximum fee a user is willing to pay for an order. Use the maker fee for Post-only orders and the taker fee for all other orders. Enter the fee in decimal format (e.g., 0.1 for 10%). To view current fees, use the
Get fees
endpoint, which displays applicable fee rates.
Expiration Timestamp: All orders, including
Fill or Kill
and
Immediate or Cancel
orders, require an expiration timestamp as a mandatory parameter. When submitting orders via the API, enter the expiration time as an epoch timestamp in milliseconds. On the Mainnet, the maximum allowable expiration time is 90 days from the order creation date. On the Testnet, 28 days from the order creation date.
Market Orders: Extended does not natively support market orders. On the UI, market orders are created as limit
Immediate-or-Cancel
orders with a price parameter set to ensure immediate execution. For example, Market Buy Orders are set at the best ask price multiplied by 1.0075, and Market Sell Orders at the best bid price multiplied by 0.9925 (subtracting 0.75%).
TPSL Orders: Orders with Take Profit and/or Stop Loss require multiple signatures.
Create or edit order
HTTP Request
POST /api/v1/user/order
Create a new order or edit (replace) an open order. When you create an order via our REST API, the initial response will confirm whether the order has been successfully accepted. Please be aware that, although rare, orders can be canceled or rejected by the Matching Engine even after acceptance at the REST API level. To receive real-time updates on your order status, subscribe to the Account updates WebSocket stream. This stream provides immediate notifications of any changes to your orders, including confirmations, cancellations, and rejections.
Currently, we support
limit
,
market
,
conditional
and
tpsl
order types via API, along with
reduce-only
and
post-only
settings. For API trading, we offer the following Time-in-force settings:
GTT
(Good till time - default) and
IOC
(Immediate or cancel). On the Mainnet, the maximum allowable expiration time for
GTT
orders is 90 days from the order creation date. On the Testnet, 28 days from the order creation date. For details on supported order types and settings, please refer to the
documentation
.
To successfully place an order, it must meet the following requirements:
Trading Rules. For detailed information, please refer to the
trading rules documentation
.
Order Cost Requirements. For detailed information, please refer to the
order cost documentation
.
Margin Schedule Requirements. For detailed information, please refer to the
margin schedule documentation
.
Price requirements, which are described below.
Price requirements
Limit Orders
Long Limit Orders: Order Price â‰¤ Mark Price * (1+Limit Order Price Cap)
Short Limit Orders: Order Price â‰¥ Mark Price * (1-Limit Order Floor Ratio)
Market Orders
Long Market Order: Order Price â‰¤ Mark Price * (1 + 5%)
Short Market Order: Order Price â‰¥ Mark Price * (1 - 5%)
Conditional Orders
Short Conditional Orders: Order Price â‰¥ Trigger price * (1-Limit Order Floor Ratio)
Long Conditional Orders: Order Price â‰¤ Trigger Price * (1+Limit Order Price Cap)
TPSL Orders
Entry order: Buy; TPSL order: Sell.
Validation
Stop loss
Take profit
Trigger price validation
Trigger price < Entry order price
Trigger price > Entry order price.
Limit price validation
Order Price â‰¥ Trigger price * (1-Limit Order Floor Ratio)
Order Price â‰¥ Trigger price * (1-Limit Order Floor Ratio)
Entry order: Sell; TPSL order: Buy.
Validation
Stop loss
Take profit
Trigger price validation
Trigger price > Entry order price.
Trigger price < Entry order price.
Limit price validation
Order Price â‰¤ Trigger Price * (1+Limit Order Price Cap)
Order Price â‰¤ Trigger Price * (1+Limit Order Price Cap)
Orders Edit
To edit (replace) an open order, add its ID as the cancelId parameter. You can edit multiple parameters at once. Editing is available for all orders except for triggered TPSL orders.
Order editing and validations:
If any updated parameter fails the validations described above, all updates will be rejected.
If validations fail at the REST API level, the initial open order remains unchanged.
In the rare event that validations pass at the REST API level but fail at the Matching Engine, both the updated order and the initial open order will be cancelled.
Editable Order Parameters:
For All Order Types (except triggered TPSL orders): Order price and Execution Order Price Type (market or limit)
For All Order Types (except untriggered entire position TPSL orders and triggered TPSL orders): Order size
For Conditional and Untriggered TPSL Orders: Trigger price
For Conditional Orders: Trigger price direction (up or down)
For Non-TPSL Orders: All TPSL parameters
Self trade protection
Self-trade protection is a mechanism that prevents orders from the same client or sub-account from executing against each other. When two such orders are about to match, the system applies the self-trade protection mode specified on the taker order to determine how to handle the potential self-match.
Value
Description
DISABLED
Self trade protection is disabled
ACCOUNT
Trades within same sub-account are disabled, trades between sub-accounts are enabled.
CLIENT
Trades within same sub-account and between sub-accounts are disabled.
Request
Request example:
Copy to Clipboard
{
"id": "e581a9ca-c3a2-4318-9706-3f36a2b858d3",
"market": "BTC-USDT",
"type": "CONDITIONAL",
"side": "BUY",
"qty": "1",
"price": "1000",
"timeInForce": "GTT",
"expiryEpochMillis": 1715884049245,
"fee": "0.0002",
"nonce": "876542",
"settlement": {
"signature": {
"r": "0x17a89cb97c64f546d2dc9189e1ef73547487b228945dcda406cd0e4b8301bd3",
"s": "0x385b65811a0fc92f109d5ebc30731efd158ee4e502945cd2fcb35a4947b045e"
},
"starkKey": "0x23830b00378d17755775b5a73a5967019222997eb2660c2dbfbc74877c2730f",
"collateralPosition": "4272448241247734333"
},
"reduceOnly": true,
"postOnly": false,
"selfTradeProtectionLevel": "ACCOUNT",
"trigger": {
"triggerPrice": "12000",
"triggerPriceType": "LAST",
"direction": "UP",
"executionPriceType": "LIMIT"
},
"tpSlType": "ORDER",
"takeProfit": {
"triggerPrice": "1050",
"triggerPriceType": "LAST",
"price": "1300",
"priceType": "LIMIT",
"settlement": {
"signature": {
"r": "0x5b45f0fb2b8e075f6a5f9b4c039ccf1c01c56aa212c63f943337b920103c3a1",
"s": "0x46133ab89d90a3ae2a3a7680d2a27e30fa015c0c4979931164c51b52b27758a"
},
"starkKey": "0x23830b00378d17755775b5a73a5967019222997eb2660c2dbfbc74877c2730f",
"collateralPosition": "4272448241247734333"
}
},
"stopLoss": {
"triggerPrice": "950",
"triggerPriceType": "LAST",
"price": "900",
"priceType": "LIMIT",
"settlement": {
"signature": {
"r": "0x5033ad23fe851d16ceec5dd99f2f0c9585c5abec3f09ec89a32a961536ba55",
"s": "0x1234ee151a8b5c68efb4adaa2eaf1dcc4a5107d4446274a69389ef8abd2dcf"
},
"starkKey": "0x23830b00378d17755775b5a73a5967019222997eb2660c2dbfbc74877c2730f",
"collateralPosition": "4272448241247734333"
}
},
"builderFee": "0.0001",
"builderId": 2017
}
Body Parameters
Parameter
Required
Type
Description
id
yes
string
Order ID assigned by user.
market
yes
string
Market name.
type
yes
string
Order type. Can be
limit
,
market
,
conditional
or
tpsl
.
side
yes
string
Order side. Can be
buy
or
sell
.
qty
yes
string
Order size in base asset.
price
yes
string
Worst accepted price in collateral asset. Note that price is optional for a tpsl type
position
.
reduceOnly
no
boolean
Whether the order should be Reduce-only.
postOnly
no
boolean
Whether the order should be Post-only.
timeInForce
yes
string
Time-in-force setting. Can be
GTT
(Good till time) or
IOC
(Immediate or cancel). This parameter will default to GTT.
expiryEpochMillis
yes
number
Timestamp (in epoch milliseconds) when the order expires if not filled. Cannot exceed 3 months from the order creation time.
fee
yes
string
Highest accepted fee for the trade, expressed in decimal format (e.g., 0.1 for 10%). Use the maker fee for Post-only orders and the taker fee for all other orders.
cancelId
no
string
External ID of the order that this order is replacing.
settlement
yes
object
StarkKey signature, including nonce and signed order parameters. For details, please refer to the
Python SDK
reference implementation.
nonce
yes
string
Nonce is part of the settlement and must be a number â‰¥1 and â‰¤2^31. Please make sure to check the Python SDK reference implementation.
selfTradeProtectionLevel
yes
string
Level of self trade protection. Can be
DISABLED
,
ACCOUNT
(default) and
CLIENT
.
trigger.triggerPrice
no
string
Price threshold for triggering a conditional order.
trigger.triggerPriceType
no
string
Type of price used for the order triggering. Can be
last
,
mark
or
index
.
trigger.triggerPriceDirection
no
string
Indicates whether the order should be triggered when the price is above or below the set trigger price. It can be
up
(the order will be triggered when the price reaches or surpasses the set trigger price) or
down
(the order will be triggered when the price reaches or drops below the set trigger price).
trigger.executionPriceType
no
string
Type of price used for the order execution. Can be
limit
or
market
.
tpSlType
no
string
TPSL type determining TPSL order size. Can be
order
or
position
.
takeProfit.triggerPrice
no
string
Take Profit Trigger price.
takeProfit.triggerPriceType
no
string
Type of price used for the Take Profit order triggering. Can be
last
,
mid
(to be added soon),
mark
or
index
.
takeProfit.price
no
string
Take Profit order price.
takeProfit.priceType
no
string
Indicates whether the Take profit order should be executed as
market
or
limit
order.
takeProfit.settlement
no
object
StarkKey signature, including nonce and signed order parameters. For details, please refer to the
Python SDK
reference implementation.
triggerPrice
no
string
Stop loss Trigger price.
stopLoss.triggerPriceType
no
string
Type of price used for the Stop Loss order triggering. Can be
last
,
mid
(to be added soon),
mark
or
index
.
stopLoss.price
no
string
Stop loss order price.
stopLoss.priceType
no
string
Indicates whether the Stop loss order should be executed as
market
or
limit
order.
stopLoss.settlement
no
object
StarkKey signature, including nonce and signed order parameters. For details, please refer to the
Python SDK
reference implementation.
builderFee
no
number
Amount that user will pay builder (an alternative ui maker) for the trade. Expressed in decimal format (e.g., 0.1 for 10%)
builderId
no
number
Builder client id that will receive the builderFee
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
{
"id"
:
1791389621914243072
,
"externalId"
:
"31097979600959341921260192820644698907062844065707793749567497227004358262"
}
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data.id
yes
number
Order ID assigned by Extended.
data.externalId
yes
string
Order ID assigned by user.
Cancel order by ID
HTTP Request
DELETE /api/v1/user/order/{id}
Cancel an individual order by Extended ID.
The cancellation process is asynchronous; the endpoint returns only the status of the cancellation.
URL Parameters
Parameter
Required
Type
Description
id
yes
number
Order to be canceled, ID assigned by Extended.
Cancel order by external id
HTTP Request
DELETE /api/v1/user/order?externalId={externalId}
Cancel an individual order by user ID.
The cancellation process is asynchronous; the endpoint returns only the status of the cancellation.
URL Parameters
Parameter
Required
Type
Description
externalId
yes
string
Order to be canceled, Order ID assigned by user.
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
Mass Cancel
HTTP Request
POST /api/v1/user/order/massCancel
Mass Cancel enables the cancellation of multiple orders by ID, by specific market, or for all orders within an account.
The cancellation process is asynchronous; the endpoint returns only the status of the cancellation request.
Although all parameters are optional, at least one must be specified.
Request example:
Copy to Clipboard
{
"orderIds"
:
[
1
,
2
],
"externalOrderIds"
:
[
"ExtId-1"
,
"ExtId-2"
],
"markets"
:
[
"BTC-USD"
,
"ETH-USD"
],
"cancelAll"
:
true
}
Body Parameters
Parameter
Required
Type
Description
markets
no
string[]
Market names where all orders should be cancelled.
cancelAll
no
boolean
Indicates whether all open orders for the account should be cancelled.
orderIds
no
number[]
Cancel by Extended IDs.
externalOrderIds
no
string[]
Cancel by external IDs.
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
Mass auto-cancel (dead man's switch)
HTTP Request
POST /api/v1/user/deadmanswitch?countdownTime={countdownTime}
The dead man's switch automatically cancels all open orders for the account at the end of the specified countdown if no Mass Auto-Cancel request is received within this timeframe. Setting the time to zero will remove any outstanding scheduled cancellations.
Positions and account status are not affected by the dead man's switch.
Request Parameters
Parameter
Required
Type
Description
countdownTime
yes
number
Time till Scheduled Mass Cancel (in seconds), should be non-negative. Setting the time to zero will remove any outstanding scheduled cancellations.
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
Bridge Config
HTTP Request
GET /api/v1/user/bridge/config
Response example:
Copy to Clipboard
{
"chains"
:
[
{
"chain"
:
"ARB"
,
"contractAddress"
:
"0x10417734001162Ea139e8b044DFe28DbB8B28ad0"
}
]
}
Returns EVM chains supported for deposits and withdrawals for EVM-wallets.
Response
Parameter
Required
Type
Description
chains
yes
object[]
List of
Chain
objects.
Chain object
Parameter
Required
Type
Description
chain
yes
string
Chain name.
contractAddress
yes
string
Bridge contract address for the chain.
Get bridge quote
HTTP Request
GET /api/v1/user/bridge/quote?chainIn=ARB&chainOut=STRK&amount=100
Response example:
Copy to Clipboard
{
"id"
:
"68aaa"
,
"fee"
:
"0.1"
}
Gets a
quote
for an EVM deposit/withdrawal.
Request Parameters
Parameter
Required
Type
Description
chainIn
yes
string
Chain where bridge will accept funds. For deposit set EVM chain, for withdrawal STRK.
chainOut
yes
string
Chain where bridge will send funds. For deposit set STRK chain, for withdrawal EVM.
amount
yes
number
Amount in USD that user should pay to bridge contract on chainIn.
Response
Parameter
Required
Type
Description
id
yes
string
Quote ID.
fee
yes
decimal
Bridge fee.
Commit quote
HTTP Request
POST /api/v1/user/bridge/quote?id=68aaa
Commits a
quote
for EVM deposit/withdrawal.
If a quote is deemed acceptable it needs to be committed before the bridge can be executed. This tells our bridge provider Rhino.fi to start watching for a transaction on the origin chain that deposits the required funds into the bridge contract. Rhino.fi will then issue a commitment ID to be used when sending the funds to be bridged.
Deposits
For EVM wallets, we support deposits and withdrawals on six major chains via the Rhino.fi bridgeâ€”Arbitrum, Ethereum, Base, Binance Smart Chain, Avalanche, and Polygonâ€”currently. Please refer to the
documentation
for transaction limits and estimated processing times.
For Starknet wallets, we support USDC deposits via on-chain interaction and through the User Interface. To deposit on-chain, invoke the Starknet contract at
0x062da0780fae50d68cecaa5a051606dc21217ba290969b302db4dd99d2e9b470
.
Extended doesn't charge fees on deposits or withdrawals, but for EVM chains, bridge fees may apply. All deposits and withdrawals are subject to gas fees.
EVM deposit requires bridging, please read
Bridge section
before proceeding.
EVM deposit consists of four steps:
1) User retrieves supported chains and bridge contracts via GET /bridge/config\.
2) User requests a quote via GET /bridge/quote\.
3) If the user accepts the bridge fee, they confirm the quote using POST /bridge/quote\.
4) Finally, the user calls the depositWithId function on the source chain\. See the
rhino.fi docs
for more details.
Incorrectly sent funds are non-recoverable by Extended.
Follow the execution order exactly as described. When calling depositWithId, use the exact same ID, amount, and token address. Test with a small amount first to ensure everything works correctly.
Withdrawals
For EVM wallets, we support deposits and withdrawals on six major chainsâ€”Arbitrum, Ethereum, Base, Binance Smart Chain, Avalanche, and Polygon. Please refer to the
documentation
for transaction limits and estimated processing times.
For Starknet wallets, we support withdrawals via the User Interface and API, as described below.
Note that Available Balance for Withdrawals = max(0, Wallet Balance + min(0,Unrealised PnL) - Initial Margin Requirements).
Extended doesn't charge fees on deposits or withdrawals, but for EVM chains, bridge fees may apply. All deposits and withdrawals are subject to gas fees. Withdrawals are only permitted to wallets that are linked to the authorised account.
EVM withdrawals
EVM withdrawals involve bridging, please read the
Bridge
section first before proceeding. The withdrawal process consists of four steps:
1) User retrieves supported chains and bridge contracts via GET /bridge/config\.
2) User requests a quote with GET /bridge/quote\.
3) If the user accepts the bridge fee, they confirm the quote using POST /bridge/quote\.
4) Finally, the user submits a Starknet withdrawal with the quoteId to the bridgeStarknetAddress associated with their account\. See
Account
for details.
Starknet withdrawals
To initiate a Starknet withdrawal, send a "Create Withdrawal" request as described below or use the corresponding SDK method, signed with a private L2 key. Starknet withdrawals are only available for accounts created with a Starknet wallet.
HTTP Request
POST /api/v1/user/withdrawal
Request
Request example:
Copy to Clipboard
{
"accountId"
:
"100006"
,
"amount"
:
"2"
,
"chainId"
:
"STRK"
,
"asset"
:
"USD"
,
"settlement"
:{
"recipient"
:
"0x00f7016a6f1281925ef584bdc1fd2276b2fef02d0741acce215bc512857030dc"
,
"positionId"
:
300006
,
"collateralId"
:
"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
,
"amount"
:
"2000000"
,
"expiration"
:{
"seconds"
:
1755690249
},
"salt"
:
93763903
,
"signature"
:{
"r"
:
"1110b06f591a5495b07c1e6ccc9478cbf2301af3a207c082be4c63fde19dd0b"
,
"s"
:
"cc93ea79708889869c94c95efdb005f0f15c16dec94a93e7efda33eaf7bcbd"
}
}
}
Body Parameters
Parameter
Required
Type
Description
chainId
yes
string
For starknet withdrawals, the type should be
STRK
.
accountId
yes
number
Source account ID.
amount
yes
string
Withdrawal amount, absolute value in collateral asset.
asset
yes
string
Collateral asset name.
settlement
yes
object
Withdrawal object StarkKey signature. For details, please refer to the
Python SDK
.
quoteId
yes
object
Bridge quote id for bridged withdrawal.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
1820796462590083072
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data
yes
number
Withdrawal ID, assigned by Extended.
Create transfer
HTTP Request
POST /api/v1/user/transfer
Create a transfer between sub-accounts associated with the same wallet.
Request
Request example:
Copy to Clipboard
{
"fromAccount"
:
3004
,
"toAccount"
:
7349
,
"amount"
:
"1000"
,
"transferredAsset"
:
"USD"
,
"settlement"
:
{
"amount"
:
1000000000
,
"assetId"
:
"0x31857064564ed0ff978e687456963cba09c2c6985d8f9300a1de4962fafa054"
,
"expirationTimestamp"
:
478932
,
"nonce"
:
758978120
,
"receiverPositionId"
:
104350
,
"receiverPublicKey"
:
"0x3895139a98a6168dc8b0db251bcd0e6dcf97fd1e96f7a87d9bd3f341753a844"
,
"senderPositionId"
:
100005
,
"senderPublicKey"
:
"0x3895139a98a6168dc8b0db251bcd0e6dcf97fd1e96f7a87d9bd3f341753a844"
,
"signature"
:
{
"r"
:
"6be1839e2ca76484a1a0fcaca9cbbe3792a23656d42ecee306c31e65aadb877"
,
"s"
:
"7b8f81258e16f0f90cd12f02e81427e54b4ebf7646e9b14b57f74c2cb44bff6"
}
}
}
Body Parameters
Parameter
Required
Type
Description
fromAccount
yes
number
Source account ID.
toAccount
yes
number
Destination account ID.
amount
yes
string
Transfer amount, absolute value in collateral asset.
transferredAsset
yes
string
Collateral asset name.
settlement
yes
object
Transfer object StarkKey signature (including nonce and transfer parameters). For details, please refer to the
Python SDK
.
Response example:
Copy to Clipboard
{
"status"
:
"OK"
,
"data"
:
{
"validSignature"
:
true
,
"id"
:
1820778187672010752
}
}
Response
Parameter
Required
Type
Description
status
yes
string
Can be
OK
or
ERROR
.
data.validSignature
yes
boolean
Indicates whether the signature is valid.
data.id
yes
number
Transfer ID assigned by Extended.
Referrals
Extended offers a referral program. The following API endpoints allow you to issue referral codes and retrieve your referral statistics.
Glossary
Referral
â€“ A client who was invited by another client.
Referee
â€“ A client who invited another client.
Affiliate
â€“ A client who successfully applied to the
Affiliate Program
.
Subaffiliate
â€“ A referred user who is also an affiliate, and was referred by an affiliate.
Referred volume
â€“ The trading volume of all clients referred by the user (non-transitive).
Rebate
â€“ The reward paid to the referee of affiliate, derived from the trading fees of his referrals.
Rebate rate
â€“ The percentage of fees paid by the referral that the referee or affiliate receive.
Rebate = rebate_rate * (trading_fees - rewards_to_other_programs)
Referral schedule
â€“ A set of rules (
tiers
) that determine the rebate rate based on the L30D referred volume.
Shared objects
Tier object
Example:
Copy to Clipboard
{
"totalVolume"
:
"0"
,
"rebateRate"
:
"0.1"
,
"volumeLimitPerReferral"
:
"0"
}
Tier
is the lowest-level object that defines the rules of the referral program.
Parameter
Required
Type
Description
totalVolume
yes
number
Minimum Last 30D referred volume for the rebate rate tier.
rebateRate
yes
number
The rebate rate.
volumeLimitPerReferral
yes
number
Maximum trading volume eligible for a fee discount per referral.
Refferal schedule object
Example:
Copy to Clipboard
{
"tiers"
:
[
{
"totalVolume"
:
"0"
,
"rebateRate"
:
"0.1"
,
"volumeLimitPerReferral"
:
"0"
}
]
}
Contains a list of
Tiers
objects.
Parameter
Required
Type
Description
tiers
yes
object[]
List of
Tiers
objects.
Refferal group object
Example:
Copy to Clipboard
{
"id"
:
1
,
"schedule"
:
{
"tiers"
:
[
{
"totalVolume"
:
"0"
,
"rebateRate"
:
"0.1"
,
"volumeLimitPerReferral"
:
"0"
}
]
},
"subaffiliateRate"
:
"0.1"
}
Contains the
Referral schedule
object and the sub-affiliate rebate rate. Each affiliate can have two types of Referral groups â€” the Main group and the Protection-period group.
Parameter
Required
Type
Description
id
yes
number
Group ID.
schedule
yes
object
Refferal schedule
object.
subaffiliateRate
yes
number
Rebate rate that referee gains from their subaffiliate referral rebates.
Affiliate object
Example:
Copy to Clipboard
{
"clientId"
:
42
,
"name"
:
"ABC"
,
"onboarded"
:
1746784655000
,
"mainGroup"
:
{
"id"
:
1
,
"schedule"
:
{
"tiers"
:
[
{
"totalVolume"
:
"0"
,
"rebateRate"
:
"0.1"
,
"volumeLimitPerReferral"
:
"0"
}
]
},
"subaffiliateRate"
:
"0"
},
"d30ReferredVolume"
:
"2000"
}
Parameter
Required
Type
Description
clientId
yes
number
Affiliate's client ID on Extended.
name
yes
string
Affiliate's name on Extended.
onboarded
yes
number
Affiliate's onboarding timestamp (Unix).
mainGroup
yes
number
Affiliate's Main
Refferal group
object.
d30ReferredVolume
yes
number
Last 30D volume of users referred by the Affiliate.
protectionPeriodGroup
no
number
Affiliate's
Refferal group
during protection period.
protectionPeriodUntil
no
number
End of protection period (Unix timestamp).
Period
Enum that specifies the time period for fetching data. Can be
DAY
,
WEEK
,
MONTH
,
YEAR
,
ALL
.
Granularity
Enum that specifies the time period for fetching data. Can be
DAY
,
WEEK
,
MONTH
.
Get affiliate data
GET /api/v1/user/affiliate
Response example:
Copy to Clipboard
{
"clientId"
:
42
,
"name"
:
"ABC"
,
"onboarded"
:
1746784655000
,
"mainGroup"
:
{
"id"
:
1
,
"schedule"
:
{
"tiers"
:
[
{
"totalVolume"
:
"0"
,
"rebateRate"
:
"0.1"
,
"volumeLimitPerReferral"
:
"0"
}
]
},
"subaffiliateRate"
:
"0"
},
"d30ReferredVolume"
:
"2000"
}
If the user is an affiliate, returns their affiliate data; otherwise, returns a 404.
Response
See
Affiliate
object in the Shared objects section of
Referrals documentation
.
Get referral status
GET /api/v1/user/referrals/status
Response example:
Copy to Clipboard
{
"active"
:
true
,
"limit"
:
10000
,
"tradedVolume"
:
100
}
Returns the userâ€™s referral program status.
Response
Parameter
Required
Type
Description
active
yes
boolean
Program is active for the user - user can issue referral codes. Can be
true
or
false
.
limit
yes
number
Trading volume required to activate the referral program.
tradedVolume
yes
number
User's current traded volume.
Get referral links
GET /api/v1/user/referrals/links
Response example:
Copy to Clipboard
[
{
"id"
:
"ABC"
,
"issuedBy"
:
42
,
"issuedAt"
:
1746785907329
,
"label"
:
"ABC"
,
"isDefault"
:
true
,
"hiddenAtUi"
:
false
,
"overallRebates"
:
"50"
}
]
Returns referral links issued by the user.
Response
Parameter
Required
Type
Description
id
yes
string
Link ID.
issuedBy
yes
number
Referral client ID.
issuedAt
yes
number
Link issue timestamp (Unix).
label
yes
string
Label added by user.
isDefault
yes
boolean
Link set as default for the client. Can be
true
or
false
.
hiddenAtUi
yes
boolean
Link is visible for the client. Can be
true
or
false
.
overallRebates
yes
number
Total rebates for the link.
Get referral dashboard
GET /api/v1/user/referrals/dashboard?period={PERIOD}
Response example:
Copy to Clipboard
{
"referralLinkToDirectKeyMetrics"
:
{
"ABC"
:
{
"rebateEarned"
:
{
"current"
:
"200"
,
"previous"
:
"100"
},
"totalFeesPaid"
:
{
"current"
:
"2000"
,
"previous"
:
"1000"
},
"tradingVolume"
:
{
"current"
:
"20000"
,
"previous"
:
"10000"
},
"activeTraders"
:
{
"current"
:
200
,
"previous"
:
100
}
}
},
"subaffiliateToKeyMetrics"
:
{
"2"
:
{
"rebateEarned"
:
{
"current"
:
"200"
,
"previous"
:
"100"
},
"subaffiliateEarnings"
:
{
"current"
:
"2500"
,
"previous"
:
"1250"
}
}
},
"activeSubaffiliates"
:
{
"current"
:
1
,
"previous"
:
0
},
"affiliates"
:
[
{
"clientId"
:
2
,
"name"
:
"RUSLAN"
,
"onboarded"
:
1746792229516
,
"mainGroup"
:
{
"id"
:
1
,
"schedule"
:
{
"tiers"
:
[
{
"totalVolume"
:
"0"
,
"rebateRate"
:
"0.1"
,
"volumeLimitPerReferral"
:
"0"
}
]
},
"subaffiliateRate"
:
"0"
}
}
],
"users"
:
[
{
"firstTradedOn"
:
1746792228516
,
"wallet"
:
"0x42...a8a91"
,
"rebate"
:
"100"
,
"tradedVolume"
:
"10000"
,
"totalFees"
:
"1000"
}
],
"daily"
:
[
{
"date"
:
"2025-05-09"
,
"subaffiliates"
:
[
{
"id"
:
2
,
"rebate"
:
"5"
,
"activeUsers"
:
2
,
"referredTradingVolume"
:
"100"
,
"earnings"
:
"10"
}
],
"links"
:
[
{
"link"
:
"ABC"
,
"rebate"
:
"10"
,
"activeUsers"
:
4
,
"referredTradingVolume"
:
"200"
,
"referredFees"
:
"20"
,
"referredL30Volume"
:
"2000"
}
]
},
{
"date"
:
"2025-05-08"
,
"subaffiliates"
:
[],
"links"
:
[]
}
],
"weekly"
:
[
{
"date"
:
"2025-05-09"
,
"subaffiliates"
:
[],
"links"
:
[]
},
{
"date"
:
"2025-05-02"
,
"subaffiliates"
:
[],
"links"
:
[]
}
],
"monthly"
:
[
{
"date"
:
"2025-05-09"
,
"subaffiliates"
:
[],
"links"
:
[]
},
{
"date"
:
"2025-04-11"
,
"subaffiliates"
:
[],
"links"
:
[]
},
{
"date"
:
"2025-04-13"
,
"subaffiliates"
:
[],
"links"
:
[]
}
]
}
Returns referral program statistic for the selected period.
Request parameters
Parameter
Required
Type
Description
period
yes
string
Requested period.
Response
The
Affiliate
object is described in the Shared objects section of
Referrals documentation
. The descriptions of other objects returned by this endpoint are provided below.
Parameter
Required
Type
Description
referralLinkToDirectKeyMetrics
yes
object
Metrics aggregated by referral codes (Map
).
subaffiliateToKeyMetrics
yes
object
Metrics aggregated by subaffiliates (Map
).
activeSubaffiliates
yes
number
Number of active subaffiliates.
affiliates
yes
object[]
List of
Affiliate
objects for subaffiliates active during the period.
users
yes
object[]
List of
UserStat
objects for users active during the period.
daily
yes
object[]
List of
AffiliateStat
objects for the period with 1 day granularity.
weekly
yes
object[]
List of
AffiliateStat
objects for the period with 1 week granularity.
monthly
yes
object[]
List of
AffiliateStat
objects for the period with 1 month granularity.
CurrentToPrevious<
T
>
Parameter
Required
Type
Description
current
yes
object
<
T
> data for current period.
previous
yes
object
<
T
> data for previous period.
DirectKeyMetrics
Parameter
Required
Type
Description
rebateEarned
yes
object
CurrentToPrevious<
Number
>. Rebates earned during the period.
totalFeesPaid
yes
object
CurrentToPrevious<
Number
>. Total amount of fees paid by referrals during the period.
tradingVolume
yes
object
CurrentToPrevious<
Number
>. Referred volume during the period.
activeTraders
yes
object
CurrentToPrevious<
Number
>. Number of active traders among referrals during the period.
SubaffiliateKeyMetrics
Parameter
Required
Type
Description
rebateEarned
yes
object
CurrentToPrevious<
Number
>. Rebates earned during the period.
subaffiliateEarnings
yes
object
CurrentToPrevious<
Number
>. Total rebates earned by subaffiliates during the period.
UserStat
Parameter
Required
Type
Description
firstTradedOn
no
number
Referral's first trade timestamp (Unix).
wallet
yes
string
Masked referral's wallet.
referredBy
no
number
User's referee.
referralLink
no
string
Referral link code used by the referral.
rebate
yes
number
Rebate.
tradedVolume
yes
number
Referral's traded volume during the period.
totalFees
yes
number
Total fees paid by the referral during the period.
AffiliateStat
Parameter
Required
Type
Description
date
yes
string
Last date of the period.
subaffiliates
yes
object[]
List of
SubaffiliateStat
objects for the period grouped by subaffiliates.
links
yes
object[]
List of
LinkStat
objects for the period grouped by links.
SubaffiliateStat
Parameter
Required
Type
Description
id
yes
number
Subaffiliate's client ID on Extended.
rebate
yes
number
Rebate earned by Subaffiliate (rebate from referrals of his referrals).
activeUsers
yes
number
Number of active traders among Subaffiliate's referrals.
referredTradingVolume
yes
number
Subaffiliate's referred volume.
earnings
yes
number
Subaffiliate's rebate.
LinkStat
Parameter
Required
Type
Description
link
yes
string
Referral link code.
rebate
yes
number
Rebate earned through the link.
activeUsers
yes
number
Count of active referrals invited through the link.
referredTradingVolume
yes
number
Volume referred through the link.
referredFees
yes
number
Total fees paid by referrals invited through the link.
referredL30Volume
yes
number
Last 30D volume referred through the link.
Use referral link
POST /api/v1/user/referrals/links
Request example:
json
{
"code": "ABC"
}
Activate referral link for the authenticated client.
Create referral link code
POST /api/v1/user/referrals
Request example:
json
{
"id": "ABC",
"isDefault": true,
"hiddenAtUi": false
}
Create referral link code.
Update referral link code
PUT /api/v1/user/referrals
Update referral link code.
Request example:
json
{
"id": "ABC",
"isDefault": true,
"hiddenAtUi": false
}
Points
Points-related endpoints let users view their earned points and leaderboard ranking.
Get Earned Points
HTTP Request
GET /api/v1/user/rewards/earned
Returns points earned by the authenticated client across all seasons and epochs.
Authentication
This endpoint requires authentication.
Response example:
json
{
"status": "OK",
"data": [
{
"seasonId": 1,
"epochRewards": [
{
"epochId": 1,
"startDate": "2023-01-01T00:00:00Z",
"endDate": "2023-01-31T23:59:59Z",
"pointsReward": "50.25"
}
]
}
]
}
Response
Parameter
Type
Description
data[].seasonId
number
The ID of the reward season.
data[].epochRewards
array
List of rewards earned in each epoch.
data[].epochRewards.epochId
number
The ID of the epoch.
data[].epochRewards.startDate
string
The start date of the epoch (ISO format).
data[].epochRewards.endDate
string
The end date of the epoch (ISO format).
data[].epochRewards.pointsReward
string
The number of points earned in the epoch.
Get points leaderboard stats
HTTP Request
GET /api/v1/user/rewards/leaderboard/stats
Returns the leaderboard statistics for the authenticated client, including total points, leaderboard rank, and points league levels.
Authentication
This endpoint requires authentication.
Response example:
json
{
"status": "OK",
"data": {
"totalPoints": "1250.75",
"rank": 42,
"tradingRewardLeague": "QUEEN",
"liquidityRewardLeague": "PAWN",
"referralRewardLeague": "KING"
}
}
Response
Parameter
Type
Description
totalPoints
string
The total number of points earned.
rank
number
The client's rank on the leaderboard.
tradingRewardLeague
string
The client's league for trading points.
liquidityRewardLeague
string
The client's league for liquidity points.
referralRewardLeague
string
The client's league for referral points.
Points league levels
The following table describes the points-league levels for
tradingRewardLeague
,
liquidityRewardLeague
, and
referralRewardLeague
.
Value
Description
KING
King league - highest tier.
QUEEN
Queen league - second-highest tier.
ROOK
Rook league - advanced tier.
KNIGHT
Knight league - intermediate tier.
PAWN
Pawn league - entry-level tier.
Public WebSocket streams
Extended offers a WebSocket API for streaming updates.
Connect to the WebSocket streams using
wss://api.starknet.extended.exchange
as the host.
The server sends pings every 15 seconds and expects a pong response within 10 seconds. Although the server does not require pings from the client, it will respond with a pong if one is received.
Order book stream
HTTP Request
GET /stream.extended.exchange/v1/orderbooks/{market}
Subscribe to the orderbooks stream for a specific market or for all available markets. If the market parameter is not submitted, the stream will include data for all available markets.
In the current version we support the following depth specifications:
Full orderbook. Push frequency: 100ms. The initial response from the stream will be a snapshot of the order book. Subsequent snapshot updates will occur every minute, while updates between snapshots are delivered in delta format, reflecting only changes since the last update. Best Bid & Ask updates are always provided as snapshots.
Best bid & ask. Push frequency: 10ms. To subscribe for Best bid & ask use
GET /stream.extended.exchange/v1/orderbooks/{market}?depth=1
. Best bid & ask updates are always snapshots.
URL Parameters
Parameter
Required
Type
Description
market
no
string
Select an individual market. If not specified, the subscription includes all markets.
Query Parameters
Parameter
Required
Type
Description
depth
no
string
Specify '1' to receive updates for best bid & ask only.
Response example:
Copy to Clipboard
{
"ts"
:
1701563440000
,
"type"
:
"SNAPSHOT"
,
"data"
:
{
"m"
:
"BTC-USD"
,
"b"
:
[
{
"p"
:
"25670"
,
"q"
:
"0.1"
}
],
"a"
:
[
{
"p"
:
"25770"
,
"q"
:
"0.1"
}
]
},
"seq"
:
1
}
Response
Parameter
Type
Description
type
string
Type of message. Can be
SNAPSHOT
or
DELTA
.
ts
number
Timestamp (in epoch milliseconds) when the system generated the data.
data.m
string
Market name.
data.t
string
Type of message. Can be
SNAPSHOT
or
DELTA
.
data.b
object[]
List of bid orders. For a snapshot, bids are sorted by price in descending order.
data.b[].p
string
Bid price.
data.b[].q
string
Bid size. For a snapshot, this represents the absolute size; for a delta, the change in size.
data.a
object[]
List of ask orders. For a snapshot, asks are sorted by price in ascending order.
data.a[].p
string
Ask price.
data.a[].q
string
Ask size. For a snapshot, this represents the absolute size; for a delta, the change in size.
seq
number
Monothonic sequence number. '1' corresponds to the first snapshot, and all subsequent numbers correspond to deltas. If a client receives a sequence out of order, it should reconnect.
Trades stream
HTTP Request
GET /stream.extended.exchange/v1/publicTrades/{market}
Subscribe to the trades stream for a specific market or for all available markets. If the market parameter is not submitted, the stream will include data for all available markets.
Historical trade data is currently available only to authorized accounts via the private REST API.
URL Parameters
Parameter
Required
Type
Description
market
no
string
Select an individual market. If not specified, the subscription includes all markets.
Response example:
Copy to Clipboard
{
"ts"
:
1701563440000
,
"data"
:
[
{
"m"
:
"BTC-USD"
,
"S"
:
"BUY"
,
"tT"
:
"TRADE"
,
"T"
:
1701563440000
,
"p"
:
"25670"
,
"q"
:
"0.1"
,
"i"
:
25124
}
],
"seq"
:
2
}
Response
Parameter
Type
Description
ts
number
Timestamp (in epoch milliseconds) when the system generated the data.
data[].m
string
Market name.
data[].S
string
Side of taker trades. Can be
BUY
or
SELL
.
data[].tT
string
Trade type. Can be
TRADE
,
LIQUIDATION
or
DELEVERAGE
.
data[].T
number
Timestamp (in epoch milliseconds) when the trade happened.
data[].p
string
Trade price.
data[].q
string
Trade quantity in base asset.
data[].i
number
Trade ID.
seq
number
Monotonic sequence: Since there are no deltas, clients can skip trades that arrive out of sequence.
Funding rates stream
HTTP Request
GET /stream.extended.exchange/v1/funding/{market}
Subscribe to the funding rates stream for a specific market or for all available markets. If the market parameter is not submitted, the stream will include data for all available markets.
For historical funding rates data, use the
Get funding rates history
endpoint.
While the funding rate is calculated every minute, it is applied only once per hour. The records include only those funding rates that were used for funding fee payments.
URL Parameters
Parameter
Required
Type
Description
market
no
string
Select an individual market. If not specified, the subscription includes all markets.
Response example:
Copy to Clipboard
{
"ts"
:
1701563440000
,
"data"
:
{
"m"
:
"BTC-USD"
,
"T"
:
1701563440000
,
"f"
:
"0.001"
},
"seq"
:
2
}
Response
Parameter
Type
Description
ts
number
Timestamp (in epoch milliseconds) when the system generated the data.
data[].m
string
Market name.
data[].T
number
Timestamp (in epoch milliseconds) when the funding rate was calculated and applied.
data[].f
string
Funding rates that were applied for funding fee payments.
seq
number
Monotonic sequence: Since there are no deltas, clients can skip funding rates that arrive out of sequence.
Candles stream
HTTP Request
GET /stream.extended.exchange/v1/candles/{market}/{candleType}?interval={interval}
Subscribe to the candles stream for a specific market.
The interval parameter should be specified in the ISO 8601 duration format. Available intervals include: P30D (Calendar month), P7D (Calendar week), PT24H, PT12H, PT8H, PT4H, PT2H, PT1H, PT30M, PT15M, PT5M and PT1M.
Trades price response example:
Copy to Clipboard
{
"ts"
:
1695738675123
,
"data"
:
[
{
"T"
:
1695738674000
,
"o"
:
"1000.0000"
,
"l"
:
"800.0000"
,
"h"
:
"2400.0000"
,
"c"
:
"2100.0000"
,
"v"
:
"10.0000"
}
],
"seq"
:
1
}
Mark and Index price response example:
Copy to Clipboard
{
"ts"
:
1695738675123
,
"data"
:
[
{
"T"
:
1695738674000
,
"o"
:
"1000.0000"
,
"l"
:
"800.0000"
,
"h"
:
"2400.0000"
,
"c"
:
"2100.0000"
}
],
"seq"
:
1
}
Available price types include:
Last price:
GET /stream.extended.exchange/v1/candles/{market}/trades?interval=PT1M
Mark price:
GET /stream.extended.exchange/v1/candles/{market}/mark-prices?interval=PT1M
Index price:
GET /stream.extended.exchange/v1/candles/{market}/index-prices?interval=PT1M
Push frequency: 1-10s.
URL Parameters
Parameter
Required
Type
Description
market
yes
string
Select an individual market.
candleType
yes
string
Price type. Can be
trades
,
mark-prices
or
index-prices
.
Query Parameters
Parameter
Required
Type
Description
interval
yes
string
Duration of candle (duration in ISO 8601).
Response
Parameter
Type
Description
ts
number
Timestamp (in epoch milliseconds) when the system generated the data.
data[].T
number
Starting timestamp (in epoch milliseconds) of the candle.
data[].o
string
Open price.
data[].c
string
Close price.
data[].h
string
Highest price.
data[].l
string
Lowest price.
data[].v
string
Trading volume (only for trade candles).
seq
number
Monothonic sequence number. '1' corresponds to the first snapshot, and all subsequent numbers correspond to deltas. If a client receives a sequence out of order, it should reconnect.
Mark price stream
HTTP Request
GET /stream.extended.exchange/v1/prices/mark/{market}
Subscribe to the mark price stream for a specific market or for all available markets. If the market parameter is not submitted, the stream will include data for all available markets.
Mark prices are used to calculate unrealized P&L and serve as the reference for liquidations. The stream provides real-time updates whenever a mark price changes.
URL Parameters
Parameter
Required
Type
Description
market
no
string
Select an individual market. If not specified, the subscription includes all markets.
Response example:
Copy to Clipboard
{
"type"
:
"MP"
,
"data"
:
{
"m"
:
"BTC-USD"
,
"p"
:
"25670"
,
"ts"
:
1701563440000
},
"ts"
:
1701563440000
,
"seq"
:
1
,
"sourceEventId"
:
null
}
Response
Parameter
Type
Description
type
string
Type identifier for mark price stream ("MP").
data.m
string
Market name.
data.p
string
Mark price value.
data.ts
number
Timestamp (in epoch milliseconds) when the price was calculated.
ts
number
Timestamp (in epoch milliseconds) when the system generated the data.
seq
number
Monotonic sequence number. Clients can use this to ensure they process messages in the correct order. If a client receives a sequence out of order, it should reconnect.
sourceEventId
number
ID of the source event that triggered this update (null for regular updates).
Index price stream
HTTP Request
GET /stream.extended.exchange/v1/prices/index/{market}
Subscribe to the index price stream for a specific market or for all available markets. If the market parameter is not submitted, the stream will include data for all available markets.
An index price is a composite spot price sourced from multiple external providers. It is used as the reference for funding-rate calculations.
URL Parameters
Parameter
Required
Type
Description
market
no
string
Select an individual market. If not specified, the subscription includes all markets.
Response example:
Copy to Clipboard
{
"type"
:
"IP"
,
"data"
:
{
"m"
:
"BTC-USD"
,
"p"
:
"25680"
,
"ts"
:
1701563440000
},
"ts"
:
1701563440000
,
"seq"
:
1
,
"sourceEventId"
:
null
}
Response
Parameter
Type
Description
type
string
Type identifier for index price stream ("IP").
data.m
string
Market name.
data.p
string
Index price value.
data.ts
number
Timestamp (in epoch milliseconds) when the price was calculated.
ts
number
Timestamp (in epoch milliseconds) when the system generated the data.
seq
number
Monotonic sequence number. Clients can use this to ensure they process messages in the correct order. If a client receives a sequence out of order, it should reconnect.
sourceEventId
number
ID of the source event that triggered this update (null for regular updates).
Private WebSocket streams
Connect to the WebSocket streams using
ws://api.starknet.extended.exchange
as the host.
The server sends pings every 15 seconds and expects a pong response within 10 seconds. Although the server does not require pings from clients, it will respond with a pong if it receives one.
Extended employs a simplified authentication scheme for API access. Authenticate by using your API key, which should be included in an HTTP header as follows:
X-Api-Key: <API_KEY_FROM_API_MANAGEMENT_PAGE_OF_UI>
.
Account updates stream
HTTP Request
GET /stream.extended.exchange/v1/account
Orders updates response example:
Copy to Clipboard
{
"type"
:
"ORDER"
,
"data"
:
{
"orders"
:
[
{
"id"
:
1791181340771614723
,
"accountId"
:
1791181340771614721
,
"externalId"
:
"-1771812132822291885"
,
"market"
:
"BTC-USD"
,
"type"
:
"LIMIT"
,
"side"
:
"BUY"
,
"status"
:
"NEW"
,
"price"
:
"12400.000000"
,
"averagePrice"
:
"13140.000000"
,
"qty"
:
"10.000000"
,
"filledQty"
:
"3.513000"
,
"payedFee"
:
"0.513000"
,
"trigger"
:
{
"triggerPrice"
:
"1220.00000"
,
"triggerPriceType"
:
"LAST"
,
"direction"
:
"UP"
,
"executionPriceType"
:
"LIMIT"
},
"tpSlType"
:
"ORDER"
,
"takeProfit"
:
{
"triggerPrice"
:
"1.00000"
,
"triggerPriceType"
:
"LAST"
,
"price"
:
"1.00000"
,
"priceType"
:
"LIMIT"
},
"stopLoss"
:
{
"triggerPrice"
:
"1.00000"
,
"triggerPriceType"
:
"LAST"
,
"price"
:
"1.00000"
,
"priceType"
:
"LIMIT"
},
"reduceOnly"
:
true
,
"postOnly"
:
false
,
"createdTime"
:
1715885888571
,
"updatedTime"
:
1715885888571
,
"expireTime"
:
1715885888571
}
]
},
"ts"
:
1715885884837
,
"seq"
:
1
}
Trades updates response example:
Copy to Clipboard
{
"type"
:
"TRADE"
,
"data"
:
{
"trades"
:
[
{
"id"
:
1784963886257016832
,
"accountId"
:
3017
,
"market"
:
"BTC-USD"
,
"orderId"
:
9223372036854775808
,
"externalOrderId"
:
"ext-1"
,
"side"
:
"BUY"
,
"price"
:
"58853.4000000000000000"
,
"qty"
:
"0.0900000000000000"
,
"value"
:
"5296.8060000000000000"
,
"fee"
:
"0.0000000000000000"
,
"tradeType"
:
"DELEVERAGE"
,
"createdTime"
:
1701563440000
,
"isTaker"
:
true
}
]
},
"ts"
:
1715885884837
,
"seq"
:
1
}
Account balance updates response example:
Copy to Clipboard
{
"type"
:
"BALANCE"
,
"data"
:
{
"balance"
:
{
"collateralName"
:
"BTC"
,
"balance"
:
"100.000000"
,
"equity"
:
"20.000000"
,
"availableForTrade"
:
"3.000000"
,
"availableForWithdrawal"
:
"4.000000"
,
"unrealisedPnl"
:
"1.000000"
,
"initialMargin"
:
"0.140000"
,
"marginRatio"
:
"1.500000"
,
"updatedTime"
:
1699976104901
,
"exposure"
:
"12751.859629"
,
"leverage"
:
"1275.1860"
}
},
"ts"
:
1715885952304
,
"seq"
:
1
}
Positions updates response example:
Copy to Clipboard
{
"type"
:
"POSITION"
,
"data"
:
{
"positions"
:
[
{
"id"
:
1791183357858545669
,
"accountId"
:
1791183357858545665
,
"market"
:
"BTC-USD"
,
"side"
:
"SHORT"
,
"leverage"
:
"5.0"
,
"size"
:
"0.3"
,
"value"
:
"12751.8596295830"
,
"openPrice"
:
"42508.00"
,
"markPrice"
:
"42506.1987652769"
,
"liquidationPrice"
:
"75816.37"
,
"margin"
:
"637.59"
,
"unrealisedPnl"
:
"100.000000"
,
"realisedPnl"
:
"200.000000"
,
"tpTriggerPrice"
:
"1600.0000"
,
"tpLimitPrice"
:
"1500.0000"
,
"slTriggerPrice"
:
"1300.0000"
,
"slLimitPrice"
:
"1250.0000"
,
"adl"
:
1
,
"createdAt"
:
1715886365748
,
"updatedAt"
:
1715886365748
}
]
},
"ts"
:
1715886365748
,
"seq"
:
1
}
Subscribe to the account updates stream.
The initial responses will include comprehensive information about the account, including balance, open positions, and open orders, i.e. everything from
GET /v1/user/balance
,
GET /v1/user/positions
,
GET /v1/user/orders
.
Subsequent responses will contain all updates related to open orders, trades, account balance or open positions in a single message.
The response attributes will align with the responses from the corresponding REST API endpoints:
Get trades
,
Get positions
,
Get open orders
and
Get balance
. Refer to the
Account section
for details.
Error responses
Unless specified otherwise for a particular endpoint and HTTP status code, the error response model follows the general
response format and includes an error code along with a descriptive message for most errors.
Error code
Error
Description
GENERAL
400
BadRequest
Invalid or missing parameters.
401
Unauthorized
Authentication failure.
403
Forbidden
Access denied.
404
NotFound
Resource not found.
422
UnprocessableEntity
Request format is correct, but data is invalid.
429
RateLimited
Number of calls from the IP address has exceeded the rate limit.
500
InternalServerError
Internal server error.
MARKET,
ASSET & ACCOUNT
1000
AssetNotFound
Asset not found.
1001
MarketNotFound
Market not found.
1002
MarketDisabled
Market is disabled.
1003
MarketGroupNotFound
Market group not found.
1004
AccountNotFound
Account not found.
1005
NotSupportedInterval
Not supported interval.
1006
UnhandledError
Application error.
1008
ClientNotFound
Client not found.
1009
ActionNotAllowed
Action is not allowed.
1010
MaintenanceMode
Maintenance mode.
1011
PostOnlyMode
Post only mode.
1012
ReduceOnlyMode
Reduce only mode.
1013
InvalidPercentage
Percentage should be between 0 and 1.
1014
MarketReduceOnly
Market is in reduce only mode, non-reduce only orders are not allowed.
LEVERAGE
UPDATE
1049
InvalidLeverageBelowMinLeverage
Leverage below min leverage.
1050
InvalidLeverageExceedsMaxLeverage
Leverage exceeds max leverage.
10501
InvalidLeverageMaxPositionValueExceeded
Max position value exceeded for new leverage.
1052
InvalidLeverageInsufficientMargin
Insufficient margin for new leverage.
1053
InvalidLeverageInvalidPrecision
Leverage has invalid precision.
STARKNET
SIGNATURES
1100
InvalidStarknetPublicKey
Invalid Starknet public key.
1101
InvalidStarknetSignature
Invalid Starknet signature.
1102
InvalidStarknetVault
Invalid Starknet vault.
ORDER
1120
OrderQtyLessThanMinTradeSize
Order quantity less than min trade size, based on market-specific trading rules.
1121
InvalidQtyWrongSizeIncrement
Invalid quantity due to the wrong size increment, based on market-specific Minimum Change in Trade Size trading rule.
1122
OrderValueExceedsMaxOrderValue
Order value exceeds max order value, based on market-specific trading rules.
1123
InvalidQtyPrecision
Invalid quantity precision, currently equals to market-specific Minimum Change in Trade Size.
1124
InvalidPriceWrongPriceMovement
Invalid price due to wrong price movement, based on market-specific Minimum Price Change trading rule.
1125
InvalidPricePrecision
Invalid price precision, currently equals to market-specific Minimum Price Change.
1126
MaxOpenOrdersNumberExceeded
Max open orders number exceeded, currently 200 orders per market.
1127
MaxPositionValueExceeded
Max position value exceeded, based on the
Margin schedule
.
1128
InvalidTradingFees
Trading fees are invalid. Refer to
Order management section
for details.
1129
InvalidPositionTpslQty
Invalid quantity for position TP/SL.
1130
MissingOrderPrice
Order price is missing.
1131
MissingTpslTrigger
TP/SL order trigger is missing.
1132
NotAllowedOrderType
Order type is not allowed.
1133
InvalidOrderParameters
Invalid order parameters.
1134
DuplicateOrder
Duplicate Order.
1135
InvalidOrderExpiration
Order expiration date must be within 90 days for the Mainnet, 28 days for the Testnet.
1136
ReduceOnlyOrderSizeExceedsPositionSize
Reduce-only order size exceeds open position size.
1137
ReduceOnlyOrderPositionIsMissing
Position is missing for a reduce-only order.
1138
ReduceOnlyOrderPositionSameSide
Position is the same side as a reduce-only order.
1139
MarketOrderMustBeIOC
Market order must have time in force IOC.
1140
OrderCostExceedsBalance
New order cost exceeds available balance.
1141
InvalidPriceAmount
Invalid price value.
1142
EditOrderNotFound
Edit order not found.
1143
MissingConditionalTrigger
Conditional order trigger is missing.
1144
PostOnlyCantBeOnConditionalMarketOrder
Conditional market order can't be Post-only.
1145
NonReduceOnlyOrdersNotAllowed
Non reduce-only orders are not allowed.
1146
TwapOrderMustBeGTT
Twap order must have time in force GTT.
1147
OpenLossExceedsEquity
Open loss exceeds equity.
1148
TPSLOpenLossExceedsEquity
TP/SL open loss exceeds equity.
GENERAL
ACCOUNT
1500
AccountNotSelected
Account not selected.
WITHDRAWAL
1600
WithdrawalAmountMustBePositive
Withdrawal amount must be positive.
1601
WithdrawalDescriptionToLong
Withdrawal description is too long.
1602
WithdrawalRequestDoesNotMatchSettlement
Withdrawal request does not match settlement.
1604
WithdrawalExpirationTimeIsTooSoon
Withdrawal expiration time is below the 14 days minimum.
1605
WithdrawalInvalidAsset
Withdrawal asset is not valid.
1607
WithdrawalBlockedForAccount
Withdrawals blocked for the account. Please contact the team on
Discord
to unblock the withdrawals.
1608
WithdrawalAccountDoesNotBelongToUser
The withdrawal address does not match the account address.
TRANSFERS
1650
InvalidVaultTransferAmount
Vault transfer amount is incorrect.
REFERRAL
CODE
1700
ReferralCodeAlreadyExist
Referral code already exist.
1701
ReferralCodeInvalid
Referral code is not valid.
1703
ReferralProgramIsNotEnabled
Referral program is not enabled.
1704
ReferralCodeAlreadyApplied
Referral code already applied.
Legacy: StarkEx SDK
SDK configuration
Copy to Clipboard
from dataclasses import dataclass
@dataclass
class EndpointConfig:
chain_rpc_url: str
api_base_url: str
stream_url: str
onboarding_url: str
signing_domain: str
collateral_asset_contract: str
asset_operations_contract: str
collateral_asset_on_chain_id: str
collateral_decimals: int
TESTNET_CONFIG_LEGACY_SIGNING_DOMAIN = EndpointConfig(
chain_rpc_url="https://rpc.sepolia.org",
api_base_url="https://api.testnet.extended.exchange/api/v1",
stream_url="wss://api.testnet.extended.exchange/stream.extended.exchange/v1",
onboarding_url="https://api.testnet.extended.exchange",
signing_domain="x10.exchange",
collateral_asset_contract="0x0c9165046063b7bcd05c6924bbe05ed535c140a1",
asset_operations_contract="0x7f0C670079147C5c5C45eef548E55D2cAc53B391",
collateral_asset_on_chain_id="0x31857064564ed0ff978e687456963cba09c2c6985d8f9300a1de4962fafa054",
collateral_decimals=6,
)
STARKEX_MAINNET_CONFIG = EndpointConfig(
chain_rpc_url="https://cloudflare-eth.com",
api_base_url="https://api.extended.exchange/api/v1",
stream_url="wss://api.extended.exchange/stream.extended.exchange/v1",
onboarding_url="https://api.extended.exchange",
signing_domain="extended.exchange",
collateral_asset_contract="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
asset_operations_contract="0x1cE5D7f52A8aBd23551e91248151CA5A13353C65",
collateral_asset_on_chain_id="0x2893294412a4c8f915f75892b395ebbf6859ec246ec365c3b1f56f47c3a0a5d",
collateral_decimals=6,
)
Extended now operates on the Starknet instance. The wind-down plan for the StarkEx instance can be found
here
. StarkEx-specific details apply only to users whose Extended account was created before August 12, 2025, and who have not yet migrated to Starknet. In all other cases, please follow the Starknet-specific logic described above.
StarkEx Python SDK:
For installation instructions, please refer to the
description
provided.
For reference implementations, explore the
examples folder
.
For SDK configuration, please refer to the
config description
.
Supported Features:
Account creation and authorisation.
Order Management.
Account Management.
Deposits, Transfers and Withdrawals.
Market Information.
json
~~~

-----
# SOURCE FILE: fastbridge\_info.html
## fastbridge\_info
~~~ text
fastbridge_info
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
200
A successful response.
object
code
int32
required
message
string
fast_bridge_limit
string
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/fastbridge/info \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: funding-rates.html
## funding-rates
~~~ text
funding-rates
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
200
A successful response.
object
code
int32
required
message
string
funding_rates
array of objects
required
funding_rates
*
object
market_id
uint8
required
exchange
string
enum
required
binance
bybit
hyperliquid
lighter
symbol
string
required
rate
double
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/funding-rates \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: fundings.html
## fundings
~~~ text
fundings
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
market_id
uint8
required
resolution
string
enum
required
1h
1d
Allowed:
1h
1d
start_timestamp
int64
required
0 to 5000000000000
end_timestamp
int64
required
0 to 5000000000000
count_back
int64
required
200
A successful response.
object
code
int32
required
message
string
resolution
string
required
fundings
array of objects
required
fundings
*
object
timestamp
int64
required
value
string
required
rate
string
required
direction
string
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
'https://mainnet.zklighter.elliot.ai/api/v1/fundings?resolution=1h'
\
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: Get Started For Programmers.html
## Get Started For Programmers
~~~ text
Get Started For Programmers
Welcome to the Lighter SDK and API Introduction. Here, we will go through everything from the system setup, to creating and cancelling all types of orders, to fetching exchange data.
Setting up an API KEY
In order to get started using the Lighter API, you must first set up an
API_KEY_PRIVATE_KEY
, as you will need it to sign any transaction you want to make. You can find how to do it in the following
example
. The
BASE_URL
will reflect if your key is generated on testnet or mainnet (for mainnet, just change the
BASE_URL
in the example to
https://mainnet.zklighter.elliot.ai
). Note that you also need to provide your
ETH_PRIVATE_KEY
.
You can setup up to 253 API keys (2 <=
API_KEY_INDEX
<= 254). The 0 index is the one reserved for the desktop, while 1 is the one reserved for the mobile. Finally, the 255 index can be used as a value for the
api_key_index
parameter of the
apikeys
method of the
AccountApi
for getting the data about all the API keys.
In case you do not know your
ACCOUNT_INDEX
, you can find it by querying the
AccountApi
for the data about your account, as shown in this
example
.
Account types
Lighter API users can operate under a Standard or Premium accounts. The Standard account is fee-less. Premium accounts pay 0.2 bps maker and 2 bps taker fees. Find out more in
Account Types
.
The Signer
In order to create a transaction (create/cancel/modify order), you need to use the
SignerClient
. Initialize with the following code:
Initialize SignerClient
client
=
lighter
.
SignerClient
(
url
=
BASE_URL
,
private_key
=
API_KEY_PRIVATE_KEY
,
account_index
=
ACCOUNT_INDEX
,
api_key_index
=
API_KEY_INDEX
)
The code for the signer can be found in the same repo, in the
signer_client.py
file. You may notice that it uses a binary for the signer: the code for it can be found in the
lighter-go
public repo, and you can compile it yourself using the
justfile
.
Nonce
When signing a transaction, you may need to provide a nonce (number used once). A nonce needs to be incremented each time you sign something. You can get the next nonce that you need to use using the
TransactionApiâ€™s
next_nonce
method or take care of incrementing it yourself. Note that each nonce is handled per
API_KEY
.
Signing a transaction
One can sign a transaction using the
SignerClientâ€™s
sign_create_order
,
sign_modify_order
,
sign_cancel_order
and its other similar methods. For actually pushing the transaction, you need to call
send_tx
or
send_tx_batch
using the
TransactionApi
. Hereâ€™s an
example
that includes such an operation.
Note that
base_amount, price
are to be passed as integers, and
client_order_index
is an unique (across all markets) identifier you provide for you to be able to reference this order later (e.g. if you want to cancel it).
The following values can be provided for the order_type parameter:
ORDER_TYPE_LIMIT
ORDER_TYPE_MARKET
ORDER_TYPE_STOP_LOSS
ORDER_TYPE_STOP_LOSS_LIMIT
ORDER_TYPE_TAKE_PROFIT
ORDER_TYPE_TAKE_PROFIT_LIMIT
ORDER_TYPE_TWAP
The following values can be provided for the time_in_force parameter:
ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
ORDER_TIME_IN_FORCE_POST_ONLY
Signer Client Useful Wrapper Functions
The SignerClient provides several functions that sign and push a type of transaction. Hereâ€™s a list of some of them:
create_order
- signs and pushes a create order transaction;
create_market_order
- signs and pushes a create order transaction for a market order;
create_cancel_order
- signs and pushes a cancel transaction for a certain order. Note that the order_index needs to equal the client_order_index of the order to cancel;
cancel_all_orders
- signs and pushes a cancel all transactions. Note that, depending on the time_in_force provided, the transaction has different consequences:
ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL - ImmediateCancelAll;
ORDER_TIME_IN_FORCE_GOOD_TILL_TIME - ScheduledCancelAll;
ORDER_TIME_IN_FORCE_POST_ONLY - AbortScheduledCancelAll.
create_auth_token_with_expiry
- creates an auth token (useful for getting data using the Api and Ws methods)
API
The SDK provides API classes that make calling the Lighter API easier. Here are some of them and the most important of their methods:
AccountApi
- provides account data
account
- get account data either by l1_address or index
accounts_by_l1_address
- get data about all the accounts (master account and subaccounts)
apikeys
- get data about the api keys of an account (use api_key_index = 255 for getting data about all the api keys)
TransactionApi
- provides transaction related data
next_nonce
- get next nonce to be used for signing a transaction using a certain api key
send_tx
- push a transaction
send_tx_batch
- push several transactions at once
OrderApi
- provides data about orders, trades and the orderbook
order_book_details
- get data about a specific marketâ€™s orderbook
order_books
- get data about all marketsâ€™ orderbooks
You can find the rest
here
. We also provide an
example
showing how to use some of these. For the methods that require an auth token, you can generate one using the
create_auth_token_with_expiry
method of the
SignerClient
(the same applies to the websockets auth).
WebSockets
Lighter also provides access to essential info using websockets. A simple version of an
WsClient
for subscribing to account and orderbook updates is implemented
here
. You can also take it as an example implementation of such a client.
To get access to more data, you will need to connect to the websockets without the provided WsClient. You can find the streams you can connect to, how to connect, and the data they provide in the
websockets
section.
Updated 11 days ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: info.html
## info
~~~ text
info
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
200
A successful response.
object
contract_address
string
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/info \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: l1Metadata.html
## l1Metadata
~~~ text
l1Metadata
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
auth
string
made optional to support header auth clients
l1_address
string
required
authorization
string
make required after integ is done
200
A successful response.
object
l1_address
string
required
can_invite
boolean
required
referral_points_percentage
string
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/l1Metadata \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: liquidations.html
## liquidations
~~~ text
liquidations
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
auth
string
made optional to support header auth clients
account_index
int64
required
market_id
uint8
Defaults to 255
cursor
string
limit
int64
required
1 to 100
authorization
string
make required after integ is done
200
A successful response.
object
code
int32
required
message
string
liquidations
array of objects
required
liquidations
*
object
id
int64
required
market_id
uint8
required
type
string
enum
required
partial
deleverage
trade
object
required
LiqTrade object
info
object
required
LiquidationInfo object
executed_at
int64
required
next_cursor
string
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/liquidations \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: logs.html
## logs
~~~ text
logs
Get logs for a specific
account
, which can point to either account indexes (main account, sub-account, public pools) or L1 addresses. This endpoint uses different
rate limits
than the others: 100 requests/60s; 300 requests/5m; 500 requests/10m.
Query Params
Name
Type
Description
limit
int64
optional, number of logs to return, 1 to 100
offset
int64
optional, pagination
Example Response
JSON
{
"tx_type"
:
"L2UpdateLeverage"
,
"hash"
:
"189068ebc6b5c7e5efda96f92842a2fafd280990692e56899a98de8c4a12a38c"
,
"time"
:
"2025-11-20T11:56:44.999846Z"
,
"pubdata"
:{
"l2_update_leverage_pubdata"
:{
"account_index"
:[
INTEGER
],
"market_index"
:
0
,
"initial_margin_fraction"
:
200
,
"margin_mode"
:
0
}
},
"pubdata_type"
:
"L2UpdateLeverage"
,
"status"
:
"executed"
}
JSON
{
"tx_type"
:
"L2Transfer"
,
"hash"
:
"189068ebc6b5c7e5efda96f92842a2fafd280990692e56899a98de8c4a12a38c"
,
"time"
:
"2025-11-19T10:54:45.817347Z"
,
"pubdata"
:{
"l2_transfer_pubdata"
:{
"from_account_index"
:[
INTEGER
],
"to_account_index"
:[
INTEGER
],
"usdc_amount"
:
"5.000000"
}
},
"pubdata_type"
:
"L2Transfer"
,
"status"
:
"executed"
}
JSON
{
"tx_type"
:
"L2CreateOrder"
,
"hash"
:
"189068ebc6b5c7e5efda96f92842a2fafd280990692e56899a98de8c4a12a38c"
,
"time"
:
"2025-11-17T21:10:44.834923Z"
,
"pubdata"
:{
"trade_pubdata_with_funding"
:{
"trade_type"
:
0
,
"market_index"
:
99
,
"is_taker_ask"
:
1
,
"maker_fee"
:
20
,
"taker_fee"
:
200
,
"taker_account_index"
:[
INTEGER
],
"maker_account_index"
:[
INTEGER
],
"fee_account_index"
:
"0"
,
"price"
:
"0.12345"
,
"size"
:
"1.0"
,
"funding_rate_prefix_sum"
:
577399884349754
}
},
"pubdata_type"
:
"TradeWithFunding"
,
"status"
:
"executed"
}
Updated 8 days ago
Did this page help you?
Yes
No
Updated 8 days ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: nextNonce.html
## nextNonce
~~~ text
nextNonce
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
account_index
int64
required
api_key_index
uint8
required
200
A successful response.
object
code
int32
required
message
string
nonce
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/nextNonce \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: Nonce Management.html
## Nonce Management
~~~ text
Nonce Management
Itâ€™s better to use multiple api keys.
You can use up to 255 different
API_KEY_INDEX
values. Each index manages its own nonce stream independently, allowing parallel transaction submission.
In general, if a transaction has a non error response, you can increase the nonce manually (nonce will NOT update right away on the server side) - the cases where there is no api error and the transaction fails internally should be very rare
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: notification\_ack.html
## notification\_ack
~~~ text
notification_ack
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
notif_id
string
required
auth
string
made optional to support header auth clients
account_index
int64
required
authorization
string
make required after integ is done
200
A successful response.
object
code
int32
required
message
string
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
POST \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/notification/ack \
3
--header
'accept: application/json'
\
4
--header
'content-type: multipart/form-data'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: orderBookDetails.html
## orderBookDetails
~~~ text
orderBookDetails
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
market_id
uint8
Defaults to 255
200
A successful response.
object
code
int32
required
message
string
order_book_details
array of objects
required
order_book_details
*
object
symbol
string
required
market_id
uint8
required
status
string
enum
required
inactive
active
taker_fee
string
required
maker_fee
string
required
liquidation_fee
string
required
min_base_amount
string
required
min_quote_amount
string
required
supported_size_decimals
uint8
required
supported_price_decimals
uint8
required
supported_quote_decimals
uint8
required
size_decimals
uint8
required
price_decimals
uint8
required
quote_multiplier
int64
required
default_initial_margin_fraction
integer
required
min_initial_margin_fraction
integer
required
maintenance_margin_fraction
integer
required
closeout_margin_fraction
integer
required
last_trade_price
double
required
daily_trades_count
int64
required
daily_base_token_volume
double
required
daily_quote_token_volume
double
required
daily_price_low
double
required
daily_price_high
double
required
daily_price_change
double
required
open_interest
double
required
daily_chart
object
required
Has additional fields
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: orderBookOrders.html
## orderBookOrders
~~~ text
orderBookOrders
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
market_id
uint8
required
limit
int64
required
1 to 100
200
A successful response.
object
code
int32
required
message
string
total_asks
int64
required
asks
array of objects
required
asks
*
object
order_index
int64
required
order_id
string
required
owner_account_index
int64
required
initial_base_amount
string
required
remaining_base_amount
string
required
price
string
required
order_expiry
int64
required
total_bids
int64
required
bids
array of objects
required
bids
*
object
order_index
int64
required
order_id
string
required
owner_account_index
int64
required
initial_base_amount
string
required
remaining_base_amount
string
required
price
string
required
order_expiry
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/orderBookOrders \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: orderBooks.html
## orderBooks
~~~ text
orderBooks
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
market_id
uint8
Defaults to 255
200
A successful response.
object
code
int32
required
message
string
order_books
array of objects
required
order_books
*
object
symbol
string
required
market_id
uint8
required
status
string
enum
required
inactive
active
taker_fee
string
required
maker_fee
string
required
liquidation_fee
string
required
min_base_amount
string
required
min_quote_amount
string
required
supported_size_decimals
uint8
required
supported_price_decimals
uint8
required
supported_quote_decimals
uint8
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/orderBooks \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: pnl.html
## pnl
~~~ text
pnl
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
auth
string
by
string
enum
required
index
Allowed:
index
value
string
required
resolution
string
enum
required
1m
5m
15m
1h
4h
1d
Allowed:
1m
5m
15m
1h
4h
1d
start_timestamp
int64
required
0 to 5000000000000
end_timestamp
int64
required
0 to 5000000000000
count_back
int64
required
ignore_transfers
boolean
Defaults to false
true
false
authorization
string
200
A successful response.
object
code
int32
required
message
string
resolution
string
required
pnl
array of objects
required
pnl
*
object
timestamp
int64
required
trade_pnl
double
required
inflow
double
required
outflow
double
required
pool_pnl
double
required
pool_inflow
double
required
pool_outflow
double
required
pool_total_shares
double
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
'https://mainnet.zklighter.elliot.ai/api/v1/pnl?by=index&resolution=1m'
\
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: positionFunding.html
## positionFunding
~~~ text
positionFunding
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
auth
is required when fetching an account_index linked to a main account or a sub-account, but can be left empty for public pools.
auth
string
account_index
int64
required
market_id
uint8
Defaults to 255
cursor
string
limit
int64
required
1 to 100
side
string
enum
Defaults to all
long
short
all
Allowed:
long
short
all
authorization
string
200
A successful response.
object
code
int32
required
message
string
position_fundings
array of objects
required
position_fundings
*
object
timestamp
int64
required
market_id
uint8
required
funding_id
int64
required
change
string
required
rate
string
required
position_size
string
required
position_side
string
enum
required
long
short
next_cursor
string
400
Bad request
object
code
int32
required
message
string
Updated 8 days ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/positionFunding \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 8 days ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: publicPoolsMetadata.html
## publicPoolsMetadata
~~~ text
publicPoolsMetadata
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
auth
string
filter
string
enum
all
user
protocol
account_index
Allowed:
all
user
protocol
account_index
index
int64
required
limit
int64
required
1 to 100
account_index
int64
authorization
string
200
A successful response.
object
code
int32
required
message
string
public_pools
array of objects
required
public_pools
*
object
code
int32
required
message
string
account_index
int64
required
account_type
uint8
required
name
string
required
l1_address
string
required
annual_percentage_yield
double
required
status
uint8
required
operator_fee
string
required
total_asset_value
string
required
total_shares
int64
required
account_share
object
PublicPoolShare object
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/publicPoolsMetadata \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: Rate Limits.html
## Rate Limits
~~~ text
Rate Limits
We enforce rate limits on both REST API and WebSocket usage. These limits applies on both IP address and L1 wallet address.
Below is an overview of our rate limiting rules:
ðŸ“Š REST API Endpoint Limits
The following limits apply to the
https://mainnet.zklighter.elliot.ai/
base URL, different limits (listed further below) apply to
https://explorer.elliot.ai/
.
Premium Account
users have limit of 24000 weighted REST API requests per minute window, while
Standard Account
users 60 per minute.
The
sendTx
and
sendTxBatch
transaction types are the only types of transactions that can increase in quota (see
Volume Quota
)
, which are used to create and modify orders. All other endpoints are have a set limit of tx per minute and do not increase with volume.
Weights per endpoint are the following:
/api/v1/sendTx
,
/api/v1/sendTxBatch
,
/api/v1/nextNonce
Per User
: 6
/api/v1/publicPools
,
/api/v1/txFromL1TxHash
,
/api/v1/candlesticks
Per User
: 50
/api/v1/accountInactiveOrders
,
/api/v1/deposit/latest
Per User
: 100
/api/v1/apikeys
Per User
: 150
Other endpoints not listed above are limited to:
Per User
: 300
ðŸŒ Standard Tier Rate Limit
Requests from a single IP address and L1 wallet address are capped at:
60 requests per minute
under the Standard Account
ðŸ“Š Explorer REST API Endpoint Limits
The following limits apply to the
https://explorer.elliot.ai/
Base URL.
Standard Users and Premium Users have the same limit of 15 weighted requests per minute window.
Weights per endpoint are the following:
/api/search
Per User
: 3
/api/accounts/param/positions
,
/api/accounts/param/logs
Per User
: 2
Other endpoints not listed above are limited to:
Per User
: 1
ðŸ”Œ WebSocket Limits
To prevent resource exhaustion, we enforce the following usage limits
per IP
:
Connections
: 100
Subscriptions per connection
: 100
Total Subscriptions
: 1000
Max Inflight Messages
: 50
Unique Accounts
: 10
ðŸ§¾ Transaction Type Limits (per user)
These limits applied for only Standard Accounts.
Transaction Type
Limit
Default
40 requests / minute
L2Withdraw
2 requests / minute
L2UpdateLeverage
1 request / minute
L2CreateSubAccount
2 requests / minute
L2CreatePublicPool
2 requests / minute
L2ChangePubKey
2 requests / 10 seconds
L2Transfer
1 request / minute
â›” Rate Limit Exceeding Behavior
If you exceed any rate limit:
You will receive an HTTP
429 Too Many Requests
error.
For WebSocket connections, excessive messages may result in disconnection.
To avoid this, please ensure your clients are implementing proper backoff and retry strategies.
Updated 6 days ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: recentTrades.html
## recentTrades
~~~ text
recentTrades
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
market_id
uint8
required
limit
int64
required
1 to 100
200
A successful response.
object
code
int32
required
message
string
next_cursor
string
trades
array of objects
required
trades
*
object
trade_id
int64
required
tx_hash
string
required
type
string
enum
required
trade
liquidation
deleverage
market_id
uint8
required
size
string
required
price
string
required
usd_amount
string
required
ask_id
int64
required
bid_id
int64
required
ask_account_id
int64
required
bid_account_id
int64
required
is_maker_ask
boolean
required
block_height
int64
required
timestamp
int64
required
taker_fee
int32
required
taker_position_size_before
string
required
taker_entry_quote_before
string
required
taker_initial_margin_fraction_before
integer
required
taker_position_sign_changed
boolean
required
maker_fee
int32
required
maker_position_size_before
string
required
maker_entry_quote_before
string
required
maker_initial_margin_fraction_before
integer
required
maker_position_sign_changed
boolean
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/recentTrades \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: referral\_points.html
## referral\_points
~~~ text
referral_points
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
auth
string
made optional to support header auth clients
account_index
int64
required
authorization
string
make required after integ is done
200
A successful response.
object
referrals
array of objects
required
referrals
*
object
l1_address
string
required
total_points
float
required
week_points
float
required
total_reward_points
float
required
week_reward_points
float
required
reward_point_multiplier
string
required
user_total_points
float
required
user_last_week_points
float
required
user_total_referral_reward_points
float
required
user_last_week_referral_reward_points
float
required
reward_point_multiplier
string
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/referral/points \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: sendTx.html
## sendTx
~~~ text
sendTx
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
tx_type
uint8
required
tx_info
string
required
price_protection
boolean
Defaults to true
true
false
200
A successful response.
object
code
int32
required
message
string
tx_hash
string
required
predicted_execution_time_ms
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
POST \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/sendTx \
3
--header
'accept: application/json'
\
4
--header
'content-type: multipart/form-data'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: sendTxBatch.html
## sendTxBatch
~~~ text
sendTxBatch
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
tx_types
string
required
tx_infos
string
required
200
A successful response.
object
code
int32
required
message
string
tx_hash
array of strings
required
tx_hash
*
predicted_execution_time_ms
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
POST \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/sendTxBatch \
3
--header
'accept: application/json'
\
4
--header
'content-type: multipart/form-data'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: status.html
## status
~~~ text
status
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
200
A successful response.
object
status
int32
required
network_id
int32
required
timestamp
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 11 days ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/ \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 11 days ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: trades.html
## trades
~~~ text
trades
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
auth
string
market_id
uint8
Defaults to 255
account_index
int64
Defaults to -1
order_index
int64
sort_by
string
enum
required
block_height
timestamp
trade_id
Allowed:
block_height
timestamp
trade_id
sort_dir
string
enum
Defaults to desc
desc
Allowed:
desc
cursor
string
from
int64
Defaults to -1
ask_filter
int8
Defaults to -1
limit
int64
required
1 to 100
authorization
string
200
A successful response.
object
code
int32
required
message
string
next_cursor
string
trades
array of objects
required
trades
*
object
trade_id
int64
required
tx_hash
string
required
type
string
enum
required
trade
liquidation
deleverage
market_id
uint8
required
size
string
required
price
string
required
usd_amount
string
required
ask_id
int64
required
bid_id
int64
required
ask_account_id
int64
required
bid_account_id
int64
required
is_maker_ask
boolean
required
block_height
int64
required
timestamp
int64
required
taker_fee
int32
required
taker_position_size_before
string
required
taker_entry_quote_before
string
required
taker_initial_margin_fraction_before
integer
required
taker_position_sign_changed
boolean
required
maker_fee
int32
required
maker_position_size_before
string
required
maker_entry_quote_before
string
required
maker_initial_margin_fraction_before
integer
required
maker_position_sign_changed
boolean
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
'https://mainnet.zklighter.elliot.ai/api/v1/trades?sort_by=block_height'
\
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: transferFeeInfo.html
## transferFeeInfo
~~~ text
transferFeeInfo
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
auth
string
account_index
int64
required
to_account_index
int64
Defaults to -1
authorization
string
200
A successful response.
object
code
int32
required
message
string
transfer_fee_usdc
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/transferFeeInfo \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: transfer\_history.html
## transfer\_history
~~~ text
transfer_history
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
account_index
int64
required
auth
string
made optional to support header auth clients
cursor
string
authorization
string
make required after integ is done
200
A successful response.
object
code
int32
required
message
string
transfers
array of objects
required
transfers
*
object
id
string
required
amount
string
required
timestamp
int64
required
type
string
enum
required
L2TransferInflow
L2TransferOutflow
L2BurnSharesInflow
L2BurnSharesOutflow
L2MintSharesInflow
L2MintSharesOutflow
from_l1_address
string
required
to_l1_address
string
required
from_account_index
int64
required
to_account_index
int64
required
tx_hash
string
required
cursor
string
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/transfer/history \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: tx.html
## tx
~~~ text
tx
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
by
string
enum
required
hash
sequence_index
Allowed:
hash
sequence_index
value
string
required
200
A successful response.
object
code
int32
required
message
string
hash
string
required
type
uint8
required
1 to 64
info
string
required
event_info
string
required
status
int64
required
transaction_index
int64
required
l1_address
string
required
account_index
int64
required
nonce
int64
required
expire_at
int64
required
block_height
int64
required
queued_at
int64
required
executed_at
int64
required
sequence_index
int64
required
parent_hash
string
required
committed_at
int64
required
verified_at
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
'https://mainnet.zklighter.elliot.ai/api/v1/tx?by=hash'
\
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: txFromL1TxHash.html
## txFromL1TxHash
~~~ text
txFromL1TxHash
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
hash
string
required
200
A successful response.
object
code
int32
required
message
string
hash
string
required
type
uint8
required
1 to 64
info
string
required
event_info
string
required
status
int64
required
transaction_index
int64
required
l1_address
string
required
account_index
int64
required
nonce
int64
required
expire_at
int64
required
block_height
int64
required
queued_at
int64
required
executed_at
int64
required
sequence_index
int64
required
parent_hash
string
required
committed_at
int64
required
verified_at
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/txFromL1TxHash \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: Volume Quota.html
## Volume Quota
~~~ text
Volume Quota
Volume Quota
gives users higher rate limits on
SendTx
and
SendTxBatch
based on trading volume and is
only available to Premium accounts now
.
For every 10 USDC of trading volume, traders receive an additional transaction limit (i.e. volume quota increases by 1).
SendTx
and
SendTxBatch
requests will return a response indicating the remaining quota, e.g. "10780 volume quota remaining.". Every 15 seconds, you get a free
SendTx
or
SendTxBatch
which won't consume volume quota (nor remaining quota). Volume quota is shared across all sub-accounts under the same L1 address.
New accounts start at 1K quota, and you can stack at most 5.000.000 TX allowance in your volume quota, which does not expire.
This differs from Rate Limits, which enforce a maximum of
24000
weight per
60
seconds (rolling minute) for premium accounts. You can check the weight of the endpoints, and standard accounts limits here:
Rate Limits
.
Updated 8 days ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: WebSocket.html
## WebSocket
~~~ text
WebSocket
Connection
URL:
wss://mainnet.zklighter.elliot.ai/stream
You can directly connect to the WebSocket server using wscat:
wscat -c 'wss://mainnet.zklighter.elliot.ai/stream'
Send Tx
You can send transactions using the websocket as follows:
JSON
{
"type"
:
"jsonapi/sendtx"
,
"data"
: {
"tx_type"
:
INTEGER
,
"tx_info"
:
...
}
}
The
tx_type
options can be found in the
SignerClient
file, while
tx_info
can be generated using the sign methods in the SignerClient.
Example:
ws_send_tx.py
Send Batch Tx
You can send batch transactions to execute up to 50 transactions in a single message.
JSON
{
"type"
:
"jsonapi/sendtxbatch"
,
"data"
: {
"tx_types"
:
"[INTEGER]"
,
"tx_infos"
:
"[tx_info]"
}
}
The
tx_type
options can be found in the
SignerClient
file, while
tx_info
can be generated using the sign methods in the SignerClient.
Example:
ws_send_batch_tx.py
Types
We first need to define some types that appear often in the JSONs.
Transaction JSON
JSON
Transaction
=
{
"hash"
:
STRING
,
"type"
:
INTEGER
,
"info"
:
STRING
,
// json object as string, attributes depending on the tx type
"event_info"
:
STRING
,
// json object as string, attributes depending on the tx type
"status"
:
INTEGER
,
"transaction_index"
:
INTEGER
,
"l1_address"
:
STRING
,
"account_index"
:
INTEGER
,
"nonce"
:
INTEGER
,
"expire_at"
:
INTEGER
,
"block_height"
:
INTEGER
,
"queued_at"
:
INTEGER
,
"executed_at"
:
INTEGER
,
"sequence_index"
:
INTEGER
,
"parent_hash"
:
STRING
}
Example:
JSON
{
"hash"
:
"0xabc123456789def"
,
"type"
:
15
,
"info"
:
"{"AccountIndex":1,"ApiKeyIndex":2,"MarketIndex":3,"Index":404,"ExpiredAt":1700000000000,"Nonce":1234,"Sig":"0xsigexample"}"
,
"event_info"
:
"{"a":1,"i":404,"u":123,"ae":""}"
,
"status"
:
2
,
"transaction_index"
:
10
,
"l1_address"
:
"0x123abc456def789"
,
"account_index"
:
101
,
"nonce"
:
12345
,
"expire_at"
:
1700000000000
,
"block_height"
:
1500000
,
"queued_at"
:
1699999990000
,
"executed_at"
:
1700000000005
,
"sequence_index"
:
5678
,
"parent_hash"
:
"0xparenthash123456"
}
Used in:
Account Tx
.
Order JSON
JSON
Order
=
{
"order_index"
:
INTEGER
,
"client_order_index"
:
INTEGER
,
"order_id"
:
STRING
,
// same as order_index but string
"client_order_id"
:
STRING
,
// same as client_order_index but string
"market_index"
:
INTEGER
,
"owner_account_index"
:
INTEGER
,
"initial_base_amount"
:
STRING
,
"price"
:
STRING
,
"nonce"
:
INTEGER
,
"remaining_base_amount"
:
STRING
,
"is_ask"
:
BOOL
,
"base_size"
:
INTEGER
,
"base_price"
:
INTEGER
,
"filled_base_amount"
:
STRING
,
"filled_quote_amount"
:
STRING
,
"side"
:
STRING
,
"type"
:
STRING
,
"time_in_force"
:
STRING
,
"reduce_only"
:
BOOL
,
"trigger_price"
:
STRING
,
"order_expiry"
:
INTEGER
,
"status"
:
STRING
,
"trigger_status"
:
STRING
,
"trigger_time"
:
INTEGER
,
"parent_order_index"
:
INTEGER
,
"parent_order_id"
:
STRING
,
"to_trigger_order_id_0"
:
STRING
,
"to_trigger_order_id_1"
:
STRING
,
"to_cancel_order_id_0"
:
STRING
,
"block_height"
:
INTEGER
,
"timestamp"
:
INTEGER
,
}
Used in:
Account Market
,
Account All Orders
,
Account Orders
.
Trade JSON
JSON
Trade
=
{
"trade_id"
:
INTEGER
,
"tx_hash"
:
STRING
,
"type"
:
STRING
,
"market_id"
:
INTEGER
,
"size"
:
STRING
,
"price"
:
STRING
,
"usd_amount"
:
STRING
,
"ask_id"
:
INTEGER
,
"bid_id"
:
INTEGER
,
"ask_account_id"
:
INTEGER
,
"bid_account_id"
:
INTEGER
,
"is_maker_ask"
:
BOOLEAN
,
"block_height"
:
INTEGER
,
"timestamp"
:
INTEGER
,
"taker_fee"
:
INTEGER
(
omitted
when
zero
),
"taker_position_size_before"
:
STRING
(
omitted
when
empty
),
"taker_entry_quote_before"
:
STRING
(
omitted
when
empty
),
"taker_initial_margin_fraction_before"
:
INTEGER
(
omitted
when
zero
),
"taker_position_sign_changed"
:
BOOL
(
omitted
when
false
),
"maker_fee"
:
INTEGER
(
omitted
when
zero
),
"maker_position_size_before"
:
STRING
(
omitted
when
empty
),
"maker_entry_quote_before"
:
STRING
(
omitted
when
empty
),
"maker_initial_margin_fraction_before"
:
INTEGER
(
omitted
when
zero
),
"maker_position_sign_changed"
:
BOOL
(
omitted
when
false
),
}
Example:
JSON
{
"trade_id"
:
401
,
"tx_hash"
:
"0xabc123456789"
,
"type"
:
"buy"
,
"market_id"
:
101
,
"size"
:
"0.5"
,
"price"
:
"20000.00"
,
"usd_amount"
:
"10000.00"
,
"ask_id"
:
501
,
"bid_id"
:
502
,
"ask_account_id"
:
123456
,
"bid_account_id"
:
654321
,
"is_maker_ask"
:
true
,
"block_height"
:
1500000
,
"timestamp"
:
1700000000
,
"taker_position_size_before"
:
"1.14880"
,
"taker_entry_quote_before"
:
"136130.046511"
,
"taker_initial_margin_fraction_before"
:
500
,
"maker_position_size_before"
:
"-0.02594"
,
"maker_entry_quote_before"
:
"3075.396750"
,
"maker_initial_margin_fraction_before"
:
400
}
Used in:
Trade
,
Account All
,
Account Market
,
Account All Trades
.
Position JSON
JSON
Position
=
{
"market_id"
:
INTEGER
,
"symbol"
:
STRING
,
"initial_margin_fraction"
:
STRING
,
"open_order_count"
:
INTEGER
,
"pending_order_count"
:
INTEGER
,
"position_tied_order_count"
:
INTEGER
,
"sign"
:
INTEGER
,
"position"
:
STRING
,
"avg_entry_price"
:
STRING
,
"position_value"
:
STRING
,
"unrealized_pnl"
:
STRING
,
"realized_pnl"
:
STRING
,
"liquidation_price"
:
STRING
,
"total_funding_paid_out"
:
STRING
(
omitted
when
empty
),
"margin_mode"
:
INT
,
"allocated_margin"
:
STRING
,
}
Example:
JSON
{
"market_id"
:
101
,
"symbol"
:
"BTC-USD"
,
"initial_margin_fraction"
:
"0.1"
,
"open_order_count"
:
2
,
"pending_order_count"
:
1
,
"position_tied_order_count"
:
3
,
"sign"
:
1
,
"position"
:
"0.5"
,
"avg_entry_price"
:
"20000.00"
,
"position_value"
:
"10000.00"
,
"unrealized_pnl"
:
"500.00"
,
"realized_pnl"
:
"100.00"
,
"liquidation_price"
:
"3024.66"
,
"total_funding_paid_out"
:
"34.2"
,
"margin_mode"
:
1
,
"allocated_margin"
:
"46342"
,
}
Used in:
Account All
,
Account Market
,
Account All Positions
.
PoolShares JSON
JSON
PoolShares
=
{
"public_pool_index"
:
INTEGER
,
"shares_amount"
:
INTEGER
,
"entry_usdc"
:
STRING
}
Example:
JSON
{
"public_pool_index"
:
1
,
"shares_amount"
:
100
,
"entry_usdc"
:
"1000.00"
}
Used in:
Account All
,
Account All Positions
.
Channels
Order Book
The order book channel sends the new ask and bid orders for the given market.
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"order_book/{MARKET_INDEX}"
}
Example Subscription
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"order_book/0"
}
Response Structure
JSON
{
"channel"
:
"order_book:{MARKET_INDEX}"
,
"offset"
:
INTEGER
,
"order_book"
: {
"code"
:
INTEGER
,
"asks"
: [
{
"price"
:
STRING
,
"size"
:
STRING
}
],
"bids"
: [
{
"price"
:
STRING
,
"size"
:
STRING
}
],
"offset"
:
INTEGER
,
"nonce"
:
INTEGER
,
"timestamp"
:
INTEGER
},
"type"
:
"update/order_book"
}
Example Response
JSON
{
"channel"
:
"order_book:0"
,
"offset"
:
41692864
,
"order_book"
: {
"code"
:
0
,
"asks"
: [
{
"price"
:
"3327.46"
,
"size"
:
"29.0915"
}
],
"bids"
: [
{
"price"
:
"3338.80"
,
"size"
:
"10.2898"
}
],
"offset"
:
41692864
},
"type"
:
"update/order_book"
}
Market Stats
The market stats channel sends the market stat data for the given market.
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"market_stats/{MARKET_INDEX}"
}
or
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"market_stats/all"
}
Example Subscription
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"market_stats/0"
}
Response Structure
JSON
{
"channel"
:
"market_stats:{MARKET_INDEX}"
,
"market_stats"
: {
"market_id"
:
INTEGER
,
"index_price"
:
STRING
,
"mark_price"
:
STRING
,
"open_interest"
:
STRING
,
"last_trade_price"
:
STRING
,
"current_funding_rate"
:
STRING
,
"funding_rate"
:
STRING
,
"funding_timestamp"
:
INTEGER
,
"daily_base_token_volume"
:
FLOAT
,
"daily_quote_token_volume"
:
FLOAT
,
"daily_price_low"
:
FLOAT
,
"daily_price_high"
:
FLOAT
,
"daily_price_change"
:
FLOAT
},
"type"
:
"update/market_stats"
}
Example Response
JSON
{
"channel"
:
"market_stats:0"
,
"market_stats"
: {
"market_id"
:
0
,
"index_price"
:
"3335.04"
,
"mark_price"
:
"3335.09"
,
"open_interest"
:
"235.25"
,
"last_trade_price"
:
"3335.65"
,
"current_funding_rate"
:
"0.0057"
,
"funding_rate"
:
"0.0005"
,
"funding_timestamp"
:
1722337200000
,
"daily_base_token_volume"
:
230206\.48999999944
,
"daily_quote_token_volume"
:
765295250\.9804002
,
"daily_price_low"
:
3265\.13
,
"daily_price_high"
:
3386\.01
,
"daily_price_change"
:
-
1\.1562612047992835
},
"type"
:
"update/market_stats"
}
Trade
The trade channel sends the new trade data for the given market.
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"trade/{MARKET_INDEX}"
}
Example Subscription
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"trade/0"
}
Response Structure
JSON
{
"channel"
:
"trade:{MARKET_INDEX}"
,
"trades"
: [
Trade
]
],
"type"
:
"update/trade"
}
Example Response
JSON
{
"channel"
:
"trade:0"
,
"trades"
: [
{
"trade_id"
:
14035051
,
"tx_hash"
:
"189068ebc6b5c7e5efda96f92842a2fafd280990692e56899a98de8c4a12a38c"
,
"type"
:
"trade"
,
"market_id"
:
0
,
"size"
:
"0.1187"
,
"price"
:
"3335.65"
,
"usd_amount"
:
"13.67"
,
"ask_id"
:
41720126
,
"bid_id"
:
41720037
,
"ask_account_id"
:
2304
,
"bid_account_id"
:
21504
,
"is_maker_ask"
:
false
,
"block_height"
:
2204468
,
"timestamp"
:
1722339648
}
],
"type"
:
"update/trade"
}
Account All
The account all channel sends specific account market data for all markets.
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"account_all/{ACCOUNT_ID}"
}
Example Subscription
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"account_all/1"
}
Response Structure
JSON
{
"account"
:
INTEGER
,
"channel"
:
"account_all:{ACCOUNT_ID}"
,
"daily_trades_count"
:
INTEGER
,
"daily_volume"
:
INTEGER
,
"weekly_trades_count"
:
INTEGER
,
"weekly_volume"
:
INTEGER
,
"monthly_trades_count"
:
INTEGER
,
"monthly_volume"
:
INTEGER
,
"total_trades_count"
:
INTEGER
,
"total_volume"
:
INTEGER
,
"funding_histories"
: {
"{MARKET_INDEX}"
: [
{
"timestamp"
:
INTEGER
,
"market_id"
:
INTEGER
,
"funding_id"
:
INTEGER
,
"change"
:
STRING
,
"rate"
:
STRING
,
"position_size"
:
STRING
,
"position_side"
:
STRING
}
]
},
"positions"
: {
"{MARKET_INDEX}"
:
Position
},
"shares"
: [
PoolShares
],
"trades"
: {
"{MARKET_INDEX}"
: [
Trade
]
},
"type"
:
"update/account_all"
}
Example Response
JSON
{
"account"
:
10
,
"channel"
:
"account_all:10"
,
"daily_trades_count"
:
123
,
"daily_volume"
:
234
,
"weekly_trades_count"
:
345
,
"weekly_volume"
:
456
,
"monthly_trades_count"
:
567
,
"monthly_volume"
:
678
,
"total_trades_count"
:
891
,
"total_volume"
:
912
,
"funding_histories"
: {
"1"
: [
{
"timestamp"
:
1700000000
,
"market_id"
:
101
,
"funding_id"
:
2001
,
"change"
:
"0.001"
,
"rate"
:
"0.0001"
,
"position_size"
:
"0.5"
,
"position_side"
:
"long"
}
]
},
"positions"
: {
"1"
: {
"market_id"
:
101
,
"symbol"
:
"BTC-USD"
,
"initial_margin_fraction"
:
"0.1"
,
"open_order_count"
:
2
,
"pending_order_count"
:
1
,
"position_tied_order_count"
:
3
,
"sign"
:
1
,
"position"
:
"0.5"
,
"avg_entry_price"
:
"20000.00"
,
"position_value"
:
"10000.00"
,
"unrealized_pnl"
:
"500.00"
,
"realized_pnl"
:
"100.00"
,
"liquidation_price"
:
"3024.66"
,
"total_funding_paid_out"
:
"34.2"
,
"margin_mode"
:
1
,
"allocated_margin"
:
"46342"
,
}
},
"shares"
: [
{
"public_pool_index"
:
1
,
"shares_amount"
:
100
,
"entry_usdc"
:
"1000.00"
}
],
"trades"
: {
"1"
: [
{
"trade_id"
:
401
,
"tx_hash"
:
"0xabc123456789"
,
"type"
:
"buy"
,
"market_id"
:
101
,
"size"
:
"0.5"
,
"price"
:
"20000.00"
,
"usd_amount"
:
"10000.00"
,
"ask_id"
:
501
,
"bid_id"
:
502
,
"ask_account_id"
:
123456
,
"bid_account_id"
:
654321
,
"is_maker_ask"
:
true
,
"block_height"
:
1500000
,
"timestamp"
:
1700000000
,
"taker_position_size_before"
:
"1.14880"
,
"taker_entry_quote_before"
:
"136130.046511"
,
"taker_initial_margin_fraction_before"
:
500
,
"maker_position_size_before"
:
"-0.02594"
,
"maker_entry_quote_before"
:
"3075.396750"
,
"maker_initial_margin_fraction_before"
:
400
}
]
},
"type"
:
"update/account"
}
Account Market
The account market channel sends specific account market data for a market.
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"account_market/{MARKET_ID}/{ACCOUNT_ID}"
,
"auth"
:
"{AUTH_TOKEN}"
}
Example Subscription
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"account_market/0/40"
,
"auth"
:
"{AUTH_TOKEN}"
}
Response Structure
JSON
{
"account"
:
INTEGER
,
"channel"
:
"account_market/{MARKET_ID}/{ACCOUNT_ID}"
,
"funding_history"
: {
"timestamp"
:
INTEGER
,
"market_id"
:
INTEGER
,
"funding_id"
:
INTEGER
,
"change"
:
STRING
,
"rate"
:
STRING
,
"position_size"
:
STRING
,
"position_side"
:
STRING
},
"orders"
: [
Order
],
"position"
:
Position
,
"trades"
: [
Trade
],
"type"
:
"update/account_market"
}
Account Stats
The account stats channel sends account stats data for the specific account.
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"user_stats/{ACCOUNT_ID}"
}
Example Subscription
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"user_stats/0"
}
Response Structure
JSON
{
"channel"
:
"user_stats:{ACCOUNT_ID}"
,
"stats"
: {
"collateral"
:
STRING
,
"portfolio_value"
:
STRING
,
"leverage"
:
STRING
,
"available_balance"
:
STRING
,
"margin_usage"
:
STRING
,
"buying_power"
:
STRING
,
"cross_stats"
:{
"collateral"
:
STRING
,
"portfolio_value"
:
STRING
,
"leverage"
:
STRING
,
"available_balance"
:
STRING
,
"margin_usage"
:
STRING
,
"buying_power"
:
STRING
},
"total_stats"
:{
"collateral"
:
STRING
,
"portfolio_value"
:
STRING
,
"leverage"
:
STRING
,
"available_balance"
:
STRING
,
"margin_usage"
:
STRING
,
"buying_power"
:
STRING
}
},
"type"
:
"update/user_stats"
}
Example Response
JSON
{
"channel"
:
"user_stats:10"
,
"stats"
: {
"collateral"
:
"5000.00"
,
"portfolio_value"
:
"15000.00"
,
"leverage"
:
"3.0"
,
"available_balance"
:
"2000.00"
,
"margin_usage"
:
"0.80"
,
"buying_power"
:
"4000.00"
,
"cross_stats"
:{
"collateral"
:
"0.000000"
,
"portfolio_value"
:
"0.000000"
,
"leverage"
:
"0.00"
,
"available_balance"
:
"0.000000"
,
"margin_usage"
:
"0.00"
,
"buying_power"
:
"0"
},
"total_stats"
:{
"collateral"
:
"0.000000"
,
"portfolio_value"
:
"0.000000"
,
"leverage"
:
"0.00"
,
"available_balance"
:
"0.000000"
,
"margin_usage"
:
"0.00"
,
"buying_power"
:
"0"
}
},
"type"
:
"update/user_stats"
}
Account Tx
This channel sends transactions related to a specific account.
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"account_tx/{ACCOUNT_ID}"
,
"auth"
:
"{AUTH_TOKEN}"
}
Response Structure
JSON
{
"channel"
:
"account_tx:{ACCOUNT_ID}"
,
"txs"
: [
Account_tx
],
"type"
:
"update/account_tx"
}
Account All Orders
The account all orders channel sends data about all the orders of an account.
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"account_all_orders/{ACCOUNT_ID}"
,
"auth"
:
"{AUTH_TOKEN}"
}
Response Structure
JSON
{
"channel"
:
"account_all_orders:{ACCOUNT_ID}"
,
"orders"
: {
"{MARKET_INDEX}"
: [
Order
]
},
"type"
:
"update/account_all_orders"
}
Height
Blockchain height updates
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"height"
,
}
Response Structure
JSON
{
"channel"
:
"height"
,
"height"
:
INTEGER
,
"type"
:
"update/height"
}
Pool data
Provides data about pool activities: trades, orders, positions, shares and funding histories.
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"pool_data/{ACCOUNT_ID}"
,
"auth"
:
"{AUTH_TOKEN}"
}
Response Structure
JSON
{
"channel"
:
"pool_data:{ACCOUNT_ID}"
,
"account"
:
INTEGER
,
"trades"
: {
"{MARKET_INDEX}"
: [
Trade
]
},
"orders"
: {
"{MARKET_INDEX}"
: [
Order
]
},
"positions"
: {
"{MARKET_INDEX}"
:
Position
},
"shares"
: [
PoolShares
],
"funding_histories"
: {
"{MARKET_INDEX}"
: [
PositionFunding
]
},
"type"
:
"subscribed/pool_data"
}
Pool info
Provides information about pools.
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"pool_info/{ACCOUNT_ID}"
,
"auth"
:
"{AUTH_TOKEN}"
}
Response Structure
JSON
{
"channel"
:
"pool_info:{ACCOUNT_ID}"
,
"pool_info"
: {
"status"
:
INTEGER
,
"operator_fee"
:
STRING
,
"min_operator_share_rate"
:
STRING
,
"total_shares"
:
INTEGER
,
"operator_shares"
:
INTEGER
,
"annual_percentage_yield"
:
FLOAT
,
"daily_returns"
: [
{
"timestamp"
:
INTEGER
,
"daily_return"
:
FLOAT
}
],
"share_prices"
: [
{
"timestamp"
:
INTEGER
,
"share_price"
:
FLOAT
}
]
},
"type"
:
"subscribed/pool_info"
}
Notification
Provides notifications received by an account. Notifications can be of three kinds: liquidation, deleverage, or announcement. Each kind has a different content structure.
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"notification/{ACCOUNT_ID}"
,
"auth"
:
"{AUTH_TOKEN}"
}
Response Structure
JSON
{
"channel"
:
"notification:{ACCOUNT_ID}"
,
"notifs"
: [
{
"id"
:
STRING
,
"created_at"
:
STRING
,
"updated_at"
:
STRING
,
"kind"
:
STRING
,
"account_index"
:
INTEGER
,
"content"
:
NotificationContent
,
"ack"
:
BOOLEAN
,
"acked_at"
:
STRING
}
],
"type"
:
"subscribed/notification"
}
Liquidation Notification Content
JSON
{
"id"
:
STRING
,
"is_ask"
:
BOOL
,
"usdc_amount"
:
STRING
,
"size"
:
STRING
,
"market_index"
:
INTEGER
,
"price"
:
STRING
,
"timestamp"
:
INTEGER
,
"avg_price"
:
STRING
}
Deleverage Notification Content
JSON
{
"id"
:
STRING
,
"usdc_amount"
:
STRING
,
"size"
:
STRING
,
"market_index"
:
INTEGER
,
"settlement_price"
:
STRING
,
"timestamp"
:
INTEGER
}
Announcement Notification Content
JSON
{
"title"
:
STRING
,
"content"
:
STRING
,
"created_at"
:
INTEGER
}
Example response
JSON
{
"channel"
:
"notification:12345"
,
"notifs"
: [
{
"id"
:
"notif_123"
,
"created_at"
:
"2024-01-15T10:30:00Z"
,
"updated_at"
:
"2024-01-15T10:30:00Z"
,
"kind"
:
"liquidation"
,
"account_index"
:
12345
,
"content"
: {
"id"
:
"notif_123"
,
"is_ask"
:
false
,
"usdc_amount"
:
"1500.50"
,
"size"
:
"0.500000"
,
"market_index"
:
1
,
"price"
:
"3000.00"
,
"timestamp"
:
1705312200
,
"avg_price"
:
"3000.00"
},
"ack"
:
false
,
"acked_at"
:
null
},
{
"id"
:
"notif_124"
,
"created_at"
:
"2024-01-15T11:00:00Z"
,
"updated_at"
:
"2024-01-15T11:00:00Z"
,
"kind"
:
"deleverage"
,
"account_index"
:
12345
,
"content"
: {
"id"
:
"notif_124"
,
"usdc_amount"
:
"500.25"
,
"size"
:
"0.200000"
,
"market_index"
:
1
,
"settlement_price"
:
"2501.25"
,
"timestamp"
:
1705314000
},
"ack"
:
false
,
"acked_at"
:
null
}
],
"type"
:
"update/notification"
}
Account Orders
The account all orders channel sends data about the orders of an account on a certain market.
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"account_orders/{MARKET_INDEX}/{ACCOUNT_ID}"
,
"auth"
:
"{AUTH_TOKEN}"
}
Response Structure
JSON
{
"account"
: {
ACCOUNT_INDEX
},
"channel"
:
"account_orders:{MARKET_INDEX}"
,
"nonce"
:
INTEGER
,
"orders"
: {
"{MARKET_INDEX}"
: [
Order
]
// the only present market index will be the one provided
},
"type"
:
"update/account_orders"
}
Account All Trades
The account all trades channel sends data about all the trades of an account.
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"account_all_trades/{ACCOUNT_ID}"
,
"auth"
:
"{AUTH_TOKEN}"
}
Response Structure
JSON
{
"channel"
:
"account_all_trades:{ACCOUNT_ID}"
,
"trades"
: {
"{MARKET_INDEX}"
: [
Trade
]
},
"total_volume"
:
FLOAT
,
"monthly_volume"
:
FLOAT
,
"weekly_volume"
:
FLOAT
,
"daily_volume"
:
FLOAT
,
"type"
:
"update/account_all_trades"
}
Account All Positions
The account all orders channel sends data about all the order of an account.
JSON
{
"type"
:
"subscribe"
,
"channel"
:
"account_all_positions/{ACCOUNT_ID}"
,
"auth"
:
"{AUTH_TOKEN}"
}
Response Structure
JSON
{
"channel"
:
"account_all_positions:{ACCOUNT_ID}"
,
"positions"
: {
"{MARKET_INDEX}"
:
Position
},
"shares"
: [
PoolShares
],
"type"
:
"update/account_all_positions"
}
Updated 7 days ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: withdrawalDelay.html
## withdrawalDelay
~~~ text
withdrawalDelay
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
200
A successful response.
object
seconds
int64
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/withdrawalDelay \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
# SOURCE FILE: withdraw\_history.html
## withdraw\_history
~~~ text
withdraw_history
Time
Status
User Agent
Make a request to see history.
URL Expired
The URL for this request expired after 30 days.
account_index
int64
required
auth
string
made optional to support header auth clients
cursor
string
filter
string
enum
all
pending
claimable
Allowed:
all
pending
claimable
authorization
string
make required after integ is done
200
A successful response.
object
code
int32
required
message
string
withdraws
array of objects
required
withdraws
*
object
id
string
required
amount
string
required
timestamp
int64
required
status
string
enum
required
failed
pending
claimable
refunded
completed
type
string
enum
required
secure
fast
l1_tx_hash
string
required
cursor
string
required
400
Bad request
object
code
int32
required
message
string
Updated 3 months ago
Did this page help you?
Yes
No
Shell
Node
Ruby
PHP
Python
xxxxxxxxxx
1
curl
--request
GET \
2
--url
https://mainnet.zklighter.elliot.ai/api/v1/withdraw/history \
3
--header
'accept: application/json'
Click
Try It!
to start a request and see the response here! Or choose an example:
application/json
200
400
Updated 3 months ago
Did this page help you?
Yes
No
~~~

-----
