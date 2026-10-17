metadata name = 'Regional OpenAI account'
metadata description = 'Entra-only IP-restricted OpenAI account with Global Standard deployments.'

@export()
@description('A model deployment with explicitly verified capacity units.')
type ModelDeployment = {
  @description('Azure deployment name used by inference requests.')
  deployment: string
  @description('Catalog model ID.')
  model: string
  @description('Pinned model version.')
  version: string
  @description('Model-specific deployment capacity units, not a universal TPM conversion.')
  @minValue(1)
  capacity: int
}

@description('Globally unique account name and custom subdomain.')
param accountName string

@description('Account resource region.')
param location string

@description('Pinned Global Standard model deployments.')
param models ModelDeployment[]

@description('Approved public IPv4 addresses or supported CIDR ranges.')
param allowedIps string[]

@description('Existing caller principal object ID.')
param callerPrincipalId string

@description('Existing caller principal type.')
param callerPrincipalType 'User' | 'ServicePrincipal' | 'Group'

resource account 'Microsoft.CognitiveServices/accounts@2026-07-01' = {
  name: accountName
  location: location
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Deny'
      ipRules: [for ip in allowedIps: {
        value: ip
      }]
    }
  }
}

@batchSize(1)
resource deployments 'Microsoft.CognitiveServices/accounts/deployments@2026-07-01' = [for model in models: {
  parent: account
  name: model.deployment
  sku: {
    name: 'GlobalStandard'
    capacity: model.capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: model.model
      version: model.version
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}]

var openAiUserRoleId = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
)

resource callerRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, callerPrincipalId, openAiUserRoleId)
  properties: {
    roleDefinitionId: openAiUserRoleId
    principalId: callerPrincipalId
    principalType: callerPrincipalType
  }
}

@description('ARM resource ID for read-only preflight.')
output resourceId string = account.id

@description('OpenAI v1 base URL for direct Entra-authenticated inference.')
output baseUrl string = 'https://${accountName}.openai.azure.com/openai/v1/'
