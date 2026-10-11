"""
Frisbo API Client — handles HTTP communication with the Frisbo Store-View API.

Edit THIS file for API endpoint changes, request/response handling, and authentication.
For data mapping changes, edit parser.py instead.
"""
import asyncio
import logging
import random
from typing import List, Optional, Dict, Any

import httpx

from app.core.config import settings
from app.services.frisbo.rate_limiter import RateLimiter
from app.services.frisbo.parser import parse_order

# Configure logger
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# One limiter per process: a client is created per org per sync, so per-client
# limiters added up with no overall cap.
_SHARED_LIMITER: Optional[RateLimiter] = None
MAX_429_RETRIES = 4


def _shared_limiter() -> RateLimiter:
    global _SHARED_LIMITER
    if _SHARED_LIMITER is None:
        _SHARED_LIMITER = RateLimiter(settings.frisbo_rate_limit)
    return _SHARED_LIMITER


class FrisboClient:
    """Client for Frisbo Store-View API."""
    
    def __init__(self, token: str = None, org_name: str = None):
        self.base_url = settings.frisbo_api_url
        self.token = token or settings.frisbo_api_token
        self.org_name = org_name or "default"
        self.rate_limiter = _shared_limiter()
        
        # Log configuration on init
        logger.info("=" * 60)
        logger.info(f"FRISBO CLIENT INITIALIZED (org: {self.org_name})")
        logger.info(f"  Base URL: {self.base_url}")
        # Never log any part of the token: the journal is readable by the awbdev group.
        logger.info(f"  Rate Limit: {settings.frisbo_rate_limit} req/sec")
        logger.info("=" * 60)
    
    def _get_headers(self) -> Dict[str, str]:
        """Get authorization headers."""
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json: Optional[Dict] = None
    ) -> Dict:
        """Make a rate-limited API request with detailed logging."""
        await self.rate_limiter.acquire()
        
        url = f"{self.base_url}{endpoint}"
        headers = self._get_headers()
        
        # Concise request log
        logger.info(f"🚀 {method} {endpoint} (org: {self.org_name})")
        logger.debug(f"  Params: {params}, Body: {json}")
        
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                for attempt in range(MAX_429_RETRIES + 1):
                    response = await client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json
                    )
                    if response.status_code != 429 or attempt == MAX_429_RETRIES:
                        break
                    retry_after = response.headers.get("Retry-After", "")
                    delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 2 ** (attempt + 1)
                    delay = min(delay, 60) * random.uniform(1.0, 1.3)
                    logger.warning(f"⏳ 429 {endpoint} (org: {self.org_name}) — retry {attempt + 1} in {delay:.1f}s")
                    await asyncio.sleep(delay)
                    await self.rate_limiter.acquire()
                
                # Parse response body
                try:
                    response_body = response.json()
                except Exception:
                    response_body = response.text
                
                # Log response
                if response.status_code >= 400:
                    logger.error(f"❌ {method} {endpoint} → {response.status_code}: {str(response_body)[:200]}")
                    response.raise_for_status()
                
                logger.info(f"✅ {method} {endpoint} → {response.status_code}")
                logger.debug(f"  Response: {str(response_body)[:500]}")
                
                return response_body if isinstance(response_body, dict) else {"data": response_body}
                
        except httpx.HTTPStatusError as e:
            logger.error(f"❌ HTTP STATUS ERROR: {e}")
            logger.error(f"  Request URL: {e.request.url}")
            logger.error(f"  Response Status: {e.response.status_code}")
            error_detail = str(e)
            try:
                error_body = e.response.json()
                logger.error(f"  Error Body: {error_body}")
                # Include body errors in exception so callers can check for "Order not found"
                if isinstance(error_body, dict) and error_body.get("errors"):
                    error_detail = "; ".join(error_body["errors"])
                elif isinstance(error_body, dict) and error_body.get("message"):
                    error_detail = error_body["message"]
            except Exception:
                logger.error(f"  Error Text: {e.response.text}")
            raise RuntimeError(error_detail) from e
        except httpx.RequestError as e:
            logger.error(f"❌ REQUEST ERROR: {e}")
            logger.error(f"  Request URL: {e.request.url if e.request else 'N/A'}")
            raise
        except Exception as e:
            logger.error(f"❌ UNEXPECTED ERROR: {type(e).__name__}: {e}")
            raise
    
    async def search_orders(
        self,
        skip: int = 0,
        limit: int = 100,
        store_uids: Optional[List[str]] = None,
        aggregated_status_keys: Optional[List[str]] = None,
        created_at_start: Optional[str] = None,
        created_at_end: Optional[str] = None,
        updated_at_start: Optional[str] = None,
        updated_at_end: Optional[str] = None
    ) -> Dict:
        """
        Search orders with filters.
        
        Args:
            aggregated_status_keys: Filter by status (e.g., 'not_generated', 'shipped')
            created_at_start: Filter orders created after this datetime (ISO format)
            created_at_end: Filter orders created before this datetime (ISO format)
            updated_at_start: Filter orders updated after this datetime (ISO format)
            updated_at_end: Filter orders updated before this datetime (ISO format)
        
        Returns:
            {"data": [...orders...], "total": int, "skip": int, "limit": int}
        """
        params = {
            "skip": skip,
            "limit": min(limit, 100)  # API max is 100
        }
        
        if store_uids:
            params["store_uids[]"] = store_uids
        
        if aggregated_status_keys:
            params["aggregated_status_keys[]"] = aggregated_status_keys
        
        # Date range filters (new Frisbo API feature)
        if created_at_start:
            params["created_at_start"] = created_at_start
        if created_at_end:
            params["created_at_end"] = created_at_end
        if updated_at_start:
            params["updated_at_start"] = updated_at_start
        if updated_at_end:
            params["updated_at_end"] = updated_at_end
        
        return await self._request("GET", "/orders/search", params=params)
    
    async def get_order(self, order_uid: str) -> Dict:
        """Get a single order by UID."""
        return await self._request("GET", f"/orders/order/{order_uid}")
    
    async def fetch_orders(
        self,
        updated_at_start: Optional[str] = None,
        updated_at_end: Optional[str] = None,
        created_at_start: Optional[str] = None,
        created_at_end: Optional[str] = None
    ) -> List[Dict]:
        """
        Fetch all orders with pagination, optionally filtered by date range.
        
        For incremental syncs, use updated_at_start to get only orders
        that have been modified since the last sync.
        
        Frisbo API returns: {"success": true, "data": {"orders": [...]}}
        
        Returns:
            List of all matching orders
        """
        all_orders = []
        skip = 0
        limit = 100
        
        filter_desc = []
        if updated_at_start:
            filter_desc.append(f"updated_at_start={updated_at_start}")
        if created_at_start:
            filter_desc.append(f"created_at_start={created_at_start}")
        
        logger.info(f"📦 Fetching orders with filters: {', '.join(filter_desc) if filter_desc else 'none'}")
        
        while True:
            result = await self.search_orders(
                skip=skip,
                limit=limit,
                updated_at_start=updated_at_start,
                updated_at_end=updated_at_end,
                created_at_start=created_at_start,
                created_at_end=created_at_end
            )
            
            # Frisbo structure: {"success": true/false, "data": {"orders": [...]}}
            orders = []
            if isinstance(result, dict):
                # Check success flag
                if result.get("success") is False:
                    logger.error(f"Frisbo API returned success=false: {result}")
                    break
                
                # Get data object
                data = result.get("data", {})
                if isinstance(data, dict):
                    # Orders are in data.orders
                    orders = data.get("orders", [])
                elif isinstance(data, list):
                    # Fallback if data is directly a list
                    orders = data
            
            if not isinstance(orders, list):
                logger.warning(f"Orders is not a list: {type(orders)}")
                orders = []
            
            logger.info(f"Fetched {len(orders)} orders (skip={skip})")
            all_orders.extend(orders)
            
            # Check if we got all orders (no more to fetch)
            if len(orders) < limit:
                break
            
            skip += limit
        
        logger.info(f"Total orders fetched: {len(all_orders)}")
        return all_orders
    
    # Keep backward compatibility
    async def fetch_all_unfulfilled_orders(self) -> List[Dict]:
        """Backward compatibility wrapper - fetches all orders."""
        return await self.fetch_orders()
    
    async def download_awb_pdf(self, url: str) -> bytes:
        """
        Download AWB PDF from a shipment URL.
        
        S3 pre-signed URLs don't accept extra auth headers,
        so only send Frisbo auth for Frisbo API domain URLs.
        
        Args:
            url: Direct URL to the AWB PDF
            
        Returns:
            PDF file bytes
        """
        await self.rate_limiter.acquire()
        
        # Only add Frisbo auth headers for Frisbo API URLs
        # S3 pre-signed URLs will reject extra Authorization headers
        headers = {}
        if self.base_url in url:
            headers = self._get_headers()
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.content
    
    async def update_order_fulfillment(
        self,
        order_reference: str,
        tracking_number: Optional[str] = None,
        courier_name: Optional[str] = None,
        fulfillment_status: str = "fulfilled",
        shipment_status: str = "created_awb",
        aggregated_status: str = "waiting_for_courier",
    ) -> Dict:
        """
        Update an order's statuses in Frisbo after AWB printing.
        
        Uses POST /orders/order endpoint (create or update).
        Pushes three status fields:
          - fulfillment_status: "fulfilled"
          - shipment_status: "created_awb" (Shipment: created awb)
          - aggregated_status: "waiting_for_courier" (Workflow: waiting for courier)
        """
        payload = {
            "order": {
                "external_identifier": order_reference,
                "reference": order_reference,
                "fulfillment_status": fulfillment_status,
                "shipment_status": shipment_status,
                "aggregated_status": aggregated_status,
            }
        }
        
        # Add tracking/fulfillment data if available
        if tracking_number:
            fulfillment = {
                "tracking_numbers": [tracking_number],
            }
            if courier_name:
                fulfillment["tracking_company"] = courier_name
            payload["order"]["fulfillments"] = [fulfillment]
        
        return await self._request(
            "POST",
            "/orders/order",
            json=payload
        )

    async def update_orders_printed_batch(self, orders_data: List[Dict]) -> Dict:
        """
        Update multiple orders' fulfillment status after AWB printing.
        
        Args:
            orders_data: List of dicts with keys:
                - reference: order reference/number
                - tracking_number: AWB tracking number (optional)
                - courier_name: courier key (optional)
        
        Returns:
            Dict with success/failure counts.
        """
        success = 0
        failed = 0
        errors = []
        
        for order_data in orders_data:
            try:
                await self.update_order_fulfillment(
                    order_reference=order_data["reference"],
                    tracking_number=order_data.get("tracking_number"),
                    courier_name=order_data.get("courier_name"),
                )
                success += 1
            except Exception as e:
                failed += 1
                errors.append(f"{order_data['reference']}: {str(e)[:100]}")
                logger.warning(
                    f"Failed to update Frisbo status for order {order_data['reference']}: {e}"
                )
        
        logger.info(
            f"Frisbo status update batch: {success} success, {failed} failed"
        )
        return {"success": success, "failed": failed, "errors": errors[:10]}
    
    # Delegate parsing to the parser module
    def parse_order(self, raw_order: Dict) -> Dict:
        """Parse raw Frisbo order into our internal format. Delegates to parser module."""
        return parse_order(raw_order)
    
    # --- ORDER STATUS MANAGEMENT ---
    
    async def mark_waiting_for_courier(self, order_uid: str) -> Dict:
        """
        Mark an order as 'waiting for courier' in Frisbo.
        
        Called after printing the AWB. This transitions the order status
        so Frisbo knows the package is ready for pickup.
        
        Uses GET /orders/order/{order_uid}/mark_waiting_for_courier
        """
        return await self._request("GET", f"/orders/order/{order_uid}/mark_waiting_for_courier")
    
    async def mark_waiting_for_pickup(self, order_uid: str) -> Dict:
        """
        Mark an order as 'waiting for pickup' in Frisbo.
        
        Used for the drop-off flow — when the seller brings
        the package to the courier, vs courier coming to pick it up.
        
        Uses GET /orders/order/{order_uid}/mark_waiting_for_pickup
        """
        return await self._request("GET", f"/orders/order/{order_uid}/mark_waiting_for_pickup")
    
    async def print_shipment(self, order_uid: str) -> Dict:
        """
        Retrieve the shipping label for an order.
        
        Returns the AWB label data/documents directly by order UID.
        This is the proper way to get a shipping label — no need to
        parse the order's shipments array to find the PDF URL.
        
        Uses GET /orders/order/{order_uid}/print_shipment
        """
        return await self._request("GET", f"/orders/order/{order_uid}/print_shipment")
    
    async def regenerate_shipment(self, order_uid: str, parcel_count: int = 1) -> Dict:
        """
        Recreate the shipping label — generates a brand new AWB from the courier.
        
        This actually contacts the courier to create a new label,
        unlike print_shipment which just retrieves the existing one.
        
        Uses POST /orders/order/{order_uid}/regenerate_shipment
        
        Args:
            order_uid: The order's UID
            parcel_count: Number of parcels (default 1)
        """
        return await self._request(
            "POST",
            f"/orders/order/{order_uid}/regenerate_shipment",
            json={"order_uid": order_uid, "parcel_count": parcel_count}
        )
    
    async def get_shipments(self, order_uid: str) -> Dict:
        """
        Get all shipping labels for an order.
        
        Returns { "shipments": [...] } with all labels.
        Useful for multi-parcel orders.
        
        Uses GET /orders/order/{order_uid}/shipments
        """
        return await self._request("GET", f"/orders/order/{order_uid}/shipments")
    
    # --- PRODUCT / INVENTORY ITEMS ---
    
    async def search_products(
        self,
        skip: int = 0,
        limit: int = 100,
        store_uids: Optional[List[str]] = None,
    ) -> Dict:
        """
        Search inventory items (products) with filters.
        
        Uses GET /inventory_items/search endpoint.
        
        Args:
            skip: Offset
            limit: Page size (max 100)
            store_uids: Filter by store UIDs
        
        Returns:
            {"success": true, "data": {"inventory_items": [...]}}
        """
        params = {
            "skip": skip,
            "limit": min(limit, 100),
        }
        
        if store_uids:
            params["store_uids[]"] = store_uids
        
        return await self._request("GET", "/inventory_items/search", params=params)
    
    async def fetch_all_products(
        self,
        store_uids: Optional[List[str]] = None,
    ) -> List[Dict]:
        """
        Fetch all products with pagination.
        
        Same pattern as fetch_orders() — paginates until all items are fetched.
        
        Returns:
            List of all inventory items
        """
        all_items = []
        skip = 0
        limit = 100
        
        logger.info(f"📦 Fetching all products{'  for stores: ' + str(store_uids) if store_uids else ''}")
        
        while True:
            result = await self.search_products(
                skip=skip,
                limit=limit,
                store_uids=store_uids,
            )
            
            items = []
            if isinstance(result, dict):
                if result.get("success") is False:
                    logger.error(f"Frisbo products API returned success=false: {result}")
                    break
                
                data = result.get("data", {})
                if isinstance(data, dict):
                    items = data.get("inventory_items", [])
                elif isinstance(data, list):
                    items = data
            
            if not isinstance(items, list):
                items = []
            
            logger.info(f"Fetched {len(items)} products (skip={skip})")
            all_items.extend(items)
            
            if len(items) < limit:
                break
            
            skip += limit
        
        logger.info(f"Total products fetched: {len(all_items)}")
        return all_items


# Singleton instance
frisbo_client = FrisboClient()
