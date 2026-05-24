import boto3

# Connect to local instance
dynamodb = boto3.resource(
    'dynamodb',
    endpoint_url="http://localhost:8000",
    region_name="us-east-1",      # Any value works for local
    aws_access_key_id="local",    # Any value works for local
    aws_secret_access_key="local" # Any value works for local
)

# List existing tables
#print(list(dynamodb.tables.all()))

# Target your specific table
table = dynamodb.Table('pr-review-local-memory')

# Delete the table
#table.delete()

# Scan the table to get all records
response = table.scan()
items = response.get('Items', [])

# Print the records
for item in items:
    print(item)
# De the records
# for item in items:
#     print(item)
#     table.delete_item(Key={'PK': item['PK']})
