import frappe
from frappe import _
from frappe.utils import cstr, cint
from frappe.utils.nestedset import get_root_of

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_item import (
	ecommerce_item,
)
from ecommerce_integrations.shopify.constants import (
	SETTING_DOCTYPE,
	MODULE_NAME,
	SHOPIFY_VARIANTS_ATTR_LIST,
)

from shopify.resources import Product

from typing import Optional
from ecommerce_integrations.shopify.connection import temp_shopify_session


class ShopifyProduct:
	def __init__(
		self, product_id: str, variant_id: Optional[str] = None, sku: Optional[str] = None
	):
		self.product_id = product_id
		self.variant_id = variant_id
		self.sku = sku
		self.shopify_setting = frappe.get_doc(SETTING_DOCTYPE)

		if not self.shopify_setting.is_enabled():
			frappe.throw(_("Can not create Shopify product when integration is disabled."))

	def is_synced(self) -> bool:
		return ecommerce_item.is_synced(
			MODULE_NAME,
			integration_item_code=self.product_id,
			variant_id=self.variant_id,
			sku=self.sku,
		)

	def get_erpnext_item(self):
		return ecommerce_item.get_erpnext_item(
			MODULE_NAME,
			integration_item_code=self.product_id,
			variant_id=self.variant_id,
			sku=self.sku,
		)

	@temp_shopify_session
	def sync_product(self):
		if not self.is_synced():
			shopify_product = Product.find(self.product_id)
			product_dict = shopify_product.to_dict()
			self._make_item(product_dict)

	def _make_item(self, product_dict):
		_add_weight_details(product_dict)

		warehouse = self.shopify_setting.warehouse

		if _has_variants(product_dict):
			attributes = self._create_attribute(product_dict)
			self._create_item(product_dict, warehouse, 1, attributes)
			self._create_item_variants(product_dict, warehouse, attributes)

		else:
			product_dict["variant_id"] = product_dict["variants"][0]["id"]
			self._create_item(product_dict, warehouse)

	def _create_attribute(self, product_dict):
		attribute = []
		# shopify item dict
		for attr in product_dict.get("options"):
			if not frappe.db.get_value("Item Attribute", attr.get("name"), "name"):
				frappe.get_doc(
					{
						"doctype": "Item Attribute",
						"attribute_name": attr.get("name"),
						"item_attribute_values": [
							{"attribute_value": attr_value, "abbr": attr_value}
							for attr_value in attr.get("values")
						],
					}
				).insert()
				attribute.append({"attribute": attr.get("name")})

			else:
				# check for attribute values
				item_attr = frappe.get_doc("Item Attribute", attr.get("name"))
				if not item_attr.numeric_values:
					self._set_new_attribute_values(item_attr, attr.get("values"))
					item_attr.save()
					attribute.append({"attribute": attr.get("name")})

				else:
					attribute.append(
						{
							"attribute": attr.get("name"),
							"from_range": item_attr.get("from_range"),
							"to_range": item_attr.get("to_range"),
							"increment": item_attr.get("increment"),
							"numeric_values": item_attr.get("numeric_values"),
						}
					)

		return attribute

	def _set_new_attribute_values(self, item_attr, values):
		for attr_value in values:
			if not any(
				(
					d.abbr.lower() == attr_value.lower()
					or d.attribute_value.lower() == attr_value.lower()
				)
				for d in item_attr.item_attribute_values
			):
				item_attr.append(
					"item_attribute_values", {"attribute_value": attr_value, "abbr": attr_value}
				)

	def _create_item(
		self, product_dict, warehouse, has_variant=0, attributes=None, variant_of=None
	):
		item_dict = {
			"variant_of": variant_of,
			"is_stock_item": 1,
			"item_code": cstr(product_dict.get("item_code")) or cstr(product_dict.get("id")),
			"item_name": product_dict.get("title", "").strip(),
			"description": product_dict.get("body_html") or product_dict.get("title"),
			"item_group": self._get_item_group(product_dict.get("product_type")),
			"has_variants": has_variant,
			"attributes": attributes or [],
			"stock_uom": product_dict.get("uom") or _("Nos"),
			"sku": product_dict.get("sku") or _get_sku(product_dict),
			"default_warehouse": warehouse,
			"image": _get_item_image(product_dict),
			"weight_uom": product_dict.get("weight_unit"),
			"weight_per_unit": product_dict.get("weight"),
			"default_supplier": self._get_supplier(product_dict),
		}

		integration_item_code = product_dict["id"]  # shopify product_id
		variant_id = product_dict.get("variant_id", "")  # shopify variant_id if has variants
		sku = item_dict["sku"]

		ecommerce_item.create_ecommerce_item(
			MODULE_NAME,
			integration_item_code,
			item_dict,
			variant_id=variant_id,
			sku=sku,
			variant_of=variant_of,
			has_variants=has_variant,
		)

	def _create_item_variants(self, product_dict, warehouse, attributes):
		template_item = ecommerce_item.get_erpnext_item(
			MODULE_NAME, integration_item_code=product_dict.get("id")
		)

		if template_item:
			for variant in product_dict.get("variants"):
				shopify_item_variant = {
					"id": product_dict.get("id"),
					"variant_id": variant.get("id"),
					"item_code": variant.get("id"),
					"title": variant.get("title"),
					"product_type": product_dict.get("product_type"),
					"sku": variant.get("sku"),
					"uom": template_item.stock_uom or _("Nos"),
					"item_price": variant.get("price"),
					"variant_id": variant.get("id"),
					"weight_unit": variant.get("weight_unit"),
					"weight": variant.get("weight"),
				}

				for i, variant_attr in enumerate(SHOPIFY_VARIANTS_ATTR_LIST):
					if variant.get(variant_attr):
						attributes[i].update(
							{
								"attribute_value": self._get_attribute_value(
									variant.get(variant_attr), attributes[i]
								)
							}
						)
				self._create_item(
					shopify_item_variant, warehouse, 0, attributes, template_item.name
				)

	def _get_attribute_value(self, variant_attr_val, attribute):
		attribute_value = frappe.db.sql(
			"""select attribute_value from `tabItem Attribute Value`
			where parent = %s and (abbr = %s or attribute_value = %s)""",
			(attribute["attribute"], variant_attr_val, variant_attr_val),
			as_list=1,
		)
		return attribute_value[0][0] if len(attribute_value) > 0 else cint(variant_attr_val)

	def _get_item_group(self, product_type=None):
		parent_item_group = get_root_of("Item Group")

		if product_type:
			if not frappe.db.get_value("Item Group", product_type, "name"):
				item_group = frappe.get_doc(
					{
						"doctype": "Item Group",
						"item_group_name": product_type,
						"parent_item_group": parent_item_group,
						"is_group": "No",
					}
				).insert()
				return item_group.name
			else:
				return product_type
		else:
			return parent_item_group

	def _get_supplier(self, product_dict):
		if product_dict.get("vendor"):
			supplier = frappe.db.sql(
				"""select name from tabSupplier
				where name = %s or shopify_supplier_id = %s """,
				(product_dict.get("vendor"), product_dict.get("vendor").lower()),
				as_list=1,
			)

			if not supplier:
				supplier = frappe.get_doc(
					{
						"doctype": "Supplier",
						"supplier_name": product_dict.get("vendor"),
						"shopify_supplier_id": product_dict.get("vendor").lower(),
						"supplier_group": self._get_supplier_group(),
					}
				).insert()
				return supplier.name
			else:
				return product_dict.get("vendor")
		else:
			return ""

	def _get_supplier_group(self):
		supplier_group = frappe.db.get_value("Supplier Group", _("Shopify Supplier"))
		if not supplier_group:
			supplier_group = frappe.get_doc(
				{"doctype": "Supplier Group", "supplier_group_name": _("Shopify Supplier")}
			).insert()
			return supplier_group.name
		return supplier_group


def _add_weight_details(product_dict):
	variants = product_dict.get("variants")
	if variants:
		product_dict["weight"] = variants[0]["weight"]
		product_dict["weight_unit"] = variants[0]["weight_unit"]


def _has_variants(product_dict) -> bool:
	options = product_dict.get("options")
	if options and "Default Title" not in options[0]["values"]:
		return True
	return False


def _get_sku(product_dict):
	if product_dict.get("variants"):
		return product_dict.get("variants")[0].get("sku")
	return ""


def _get_item_image(product_dict):
	if product_dict.get("image"):
		return product_dict.get("image").get("src")
	return None


def create_items_if_not_exist(order):
	"""Using shopify order, sync all items that are not already synced."""
	for item in order.get("line_items", []):

		product_id = item["product_id"]
		sku = item.get("sku")
		product = ShopifyProduct(product_id, sku=sku)

		if not product.is_synced():
			product.sync_product()