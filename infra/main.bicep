metadata name = 'Foundry Global Standard multi-region reference'
metadata description = 'Optional single-subscription model resources; no gateway or Agent hosting.'

import { ModelDeployment } from './regional-account.bicep'

@description('Globally unique account name prefix.')
@minLength(3)
@maxLength(20)
param prefix string

@description('Distinct resource regions, after checking actual model availability.')
@minLength(2)
param locations string[]

@description('Explicit model names, pinned versions and model-specific capacity units.')
@minLength(1)
param models ModelDeployment[]

@description('Public IPv4 addresses or supported CIDR ranges allowed to call the resource.')
@minLength(1)
param allowedIps string[]

@description('Existing caller managed identity or developer principal object ID.')
param callerPrincipalId string

@description('The type of the existing caller principal.')
@allowed([
  'User'
  'ServicePrincipal'
  'Group'
])
param callerPrincipalType string = 'ServicePrincipal'

module regionalAccounts './regional-account.bicep' = [for location in locations: {
  name: 'models-${location}'
  params: {
    accountName: '${prefix}-${location}-${uniqueString(resourceGroup().id)}'
    location: location
    models: models
    allowedIps: allowedIps
    callerPrincipalId: callerPrincipalId
    callerPrincipalType: callerPrincipalType
  }
}]

@description('Regional account IDs and endpoints for explicit application configuration.')
output accounts array = [for (location, index) in locations: {
  region: location
  resourceId: regionalAccounts[index].outputs.resourceId
  baseUrl: regionalAccounts[index].outputs.baseUrl
}]
