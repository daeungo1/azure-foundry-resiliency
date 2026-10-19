using './main.bicep'

param prefix = 'replace-lab'
// Aliases only. Replace with actual Azure locations in an ignored *.local.bicepparam copy.
param locations = [
  'Region01'
  'Region02'
  'Region03'
  'Region04'
]
param allowedIps = [
  '203.0.113.10/32'
]
param callerPrincipalId = '00000000-0000-0000-0000-000000000000'
param callerPrincipalType = 'User'

// Capacity values are illustrative units. Verify SKU minimums and quota before deploying.
param models = [
  {
    deployment: 'gpt-6.1-sol'
    model: 'gpt-6.1-sol'
    version: '2026-09-29'
    capacity: 1
  }
  {
    deployment: 'gpt-6-luna'
    model: 'gpt-6-luna'
    version: '2026-09-22'
    capacity: 1
  }
  {
    deployment: 'gpt-5.6-terra'
    model: 'gpt-5.6-terra'
    version: '2026-07-09'
    capacity: 1
  }
  {
    deployment: 'gpt-5.6-luna'
    model: 'gpt-5.6-luna'
    version: '2026-07-09'
    capacity: 1
  }
]
