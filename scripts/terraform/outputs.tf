output "public_ip_address_1" {
  value = azurerm_linux_virtual_machine.aiopslab_vm_1.public_ip_address
}

output "public_ip_address_2" {
  value = azurerm_linux_virtual_machine.aiopslab_vm_2.public_ip_address
}

output "key_data_1" {
  value = azapi_resource_action.aiopslab_ssh_public_key_gen_1.output.privateKey
}

output "key_data_2" {
  value = azapi_resource_action.aiopslab_ssh_public_key_gen_2.output.privateKey
}

output "username" {
  value = var.username
}