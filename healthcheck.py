import os
import json
import logging
import boto3
import requests # Make sure 'requests' library is bundled or in a layer
from botocore.exceptions import ClientError

# --- Global Configuration and Clients ---
logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

ssm_client = boto3.client('ssm')
cloudwatch_client = boto3.client('cloudwatch')
sns_client = boto3.client('sns') # Initialize SNS client

# --- Environment Variables (Paths to SSM parameters and SNS Topic ARN) ---
SWITCHOVER_FLAG_SSM_PATH = os.environ.get('SWITCHOVER_FLAG_SSM_PATH')
SERVICE_HEALTH_ENDPOINTS_SSM_PATH = os.environ.get('SERVICE_HEALTH_ENDPOINTS_SSM_PATH')
SNS_TOPIC_ARN_FOR_ALERTS = os.environ.get('SNS_TOPIC_ARN_FOR_ALERTS') # New environment variable

CLOUDWATCH_NAMESPACE = os.environ.get('CLOUDWATCH_NAMESPACE', 'CTSI/HealthChecks')
CLOUDWATCH_METRIC_NAME = os.environ.get('CLOUDWATCH_METRIC_NAME', 'BinaryHealthCheck')
CLOUDWATCH_METRIC_UNIT = os.environ.get('CLOUDWATCH_METRIC_UNIT', 'Count')
CLOUDWATCH_DIMENSIONS = os.environ.get('CLOUDWATCH_DIMENSIONS', '') # Expects JSON string ""

# --- Helper Functions (Modular Format) ---

def get_ssm_parameter(param_path):
    """Retrieves a parameter value from AWS SSM Parameter Store."""
    try:
        response = ssm_client.get_parameter(Name=param_path, WithDecryption=True)
        return response['Parameter']['Value']
    except ClientError as e:
        logger.error(f"Error fetching SSM parameter '{param_path}': {e}")
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred while fetching SSM parameter '{param_path}': {e}")
        raise

def publish_cloudwatch_metric(namespace, metric_name, value, unit, dimensions):
    """Publishes a custom metric to CloudWatch."""
    try:
        cloudwatch_client.put_metric_data(
            Namespace=namespace,
            MetricData=
        )
        logger.info(f"Published metric '{metric_name}' (Value: {value}, Unit: {unit}) to namespace '{namespace}' with dimensions {dimensions}")
    except ClientError as e:
        logger.error(f"AWS API Error publishing CloudWatch metric '{metric_name}': {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred while publishing CloudWatch metric: {e}", exc_info=True)

def check_service_health(service_name, url):
    """Performs an HTTP GET request to a service health endpoint."""
    try:
        response = requests.get(url, timeout=5) # 5-second timeout
        if response.status_code == 200:
            logger.info(f"Service '{service_name}' health check passed (200 OK) at {url}")
            return True
        else:
            logger.warning(f"Service '{service_name}' health check failed: Status {response.status_code} at {url}")
            return False
    except requests.exceptions.Timeout:
        logger.error(f"Service '{service_name}' health check timed out after 5s at {url}")
        return False
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Service '{service_name}' health check connection error at {url}: {e}")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Service '{service_name}' health check failed with unexpected error at {url}: {e}")
        return False
    except Exception as e:
        logger.error(f"An unexpected error occurred during HTTP check for '{service_name}' at {url}: {e}", exc_info=True)
        return False

def send_sns_notification(subject, message):
    """Sends an email notification via SNS."""
    if not SNS_TOPIC_ARN_FOR_ALERTS:
        logger.warning("SNS_TOPIC_ARN_FOR_ALERTS is not set. Skipping SNS notification.")
        return

    try:
        response = sns_client.publish(
            TopicArn=SNS_TOPIC_ARN_FOR_ALERTS,
            Subject=subject,
            Message=message
        )
        logger.info(f"SNS notification sent. MessageId: {response['MessageId']}")
    except ClientError as e:
        logger.error(f"AWS API Error sending SNS notification: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred while sending SNS notification: {e}", exc_info=True)

def parse_dimensions(dim_str):
    """Parses a JSON string of dimensions into a list of dictionaries."""
    try:
        dims = json.loads(dim_str)
        if not isinstance(dims, list):
            raise ValueError("Dimensions must be a JSON array.")
        for dim in dims:
            if not all(k in dim for k in ['Name', 'Value']):
                raise ValueError("Each dimension object must have 'Name' and 'Value'.")
        return dims
    except json.JSONDecodeError:
        logger.error(f"Failed to decode CLOUDWATCH_DIMENSIONS: {dim_str}. Using empty dimensions.")
        return
    except ValueError as e:
        logger.error(f"Invalid CLOUDWATCH_DIMENSIONS format: {e}. Using empty dimensions.")
        return

# --- Main Lambda Handler ---

def lambda_handler(event, context):
    logger.info("Starting custom service health check Lambda invocation.")

    # Initialize values for the final published metric and status
    final_published_metric_value = 0 # Default to unhealthy
    overall_status = "UNHEALTHY"
    status_code = 500
    actual_health_status = 0 # Initialize actual health status
    
    # Parse dimensions once
    dimensions = parse_dimensions(CLOUDWATCH_DIMENSIONS)

    notification_subject = "Health Check Alert: UNKNOWN STATUS"
    notification_message = "The health check Lambda encountered an unexpected error or configuration issue."

    try:
        # 1. Fetch Control Parameters from SSM
        switchover_flag = get_ssm_parameter(SWITCHOVER_FLAG_SSM_PATH).lower()
        logger.info(f"Fetched switchover_flag: '{switchover_flag}' from SSM path: {SWITCHOVER_FLAG_SSM_PATH}")
        
        service_endpoints_str = get_ssm_parameter(SERVICE_HEALTH_ENDPOINTS_SSM_PATH)
        service_endpoints = json.loads(service_endpoints_str) # Expects JSON array of objects
        
        if not isinstance(service_endpoints, list) or not service_endpoints:
            logger.error("SERVICE_HEALTH_ENDPOINTS_SSM_PATH does not contain a valid non-empty JSON array of service endpoints. This will result in an unhealthy status.")
            actual_health_status = 0 # Treat as unhealthy if config is bad
            notification_subject = "Health Check Alert: CRITICAL - Invalid Service Endpoints Config"
            notification_message = f"The SSM parameter '{SERVICE_HEALTH_ENDPOINTS_SSM_PATH}' is misconfigured or empty. Please check the JSON format. Health check cannot proceed."
        else:
            # 2. ALWAYS Perform Automated Health Checks (for internal visibility)
            logger.info("Performing automated health checks for configured services.")
            all_services_healthy = True
            failed_services =
            for service_config in service_endpoints:
                service_name = service_config.get('name', 'unknown-service')
                service_url = service_config.get('url')
                
                if not service_url:
                    logger.error(f"Service '{service_name}' has no URL configured. Marking overall unhealthy for actual status.")
                    all_services_healthy = False
                    failed_services.append(f"Misconfigured: {service_name} (no URL)")
                    # Continue to check other services if possible, but overall is already failed
                elif not check_service_health(service_name, service_url):
                    all_services_healthy = False
                    failed_services.append(f"Failed: {service_name} at {service_url}")
            
            actual_health_status = 1 if all_services_healthy else 0
            logger.info(f"Actual health check result (irrespective of flag): {actual_health_status} ({'HEALTHY' if actual_health_status == 1 else 'UNHEALTHY'}).")

        # 3. Apply Switchover Flag Logic to Determine Final Published Metric Value
        if switchover_flag == 'force_healthy':
            final_published_metric_value = 1
            overall_status = "HEALTHY"
            status_code = 200
            logger.info(f"Health forced to HEALTHY by SSM flag '{switchover_flag}'. Actual health was {actual_health_status}.")
            notification_subject = "Health Check Info: Status Forced Healthy"
            notification_message = f"Health check status is manually forced to HEALTHY via SSM flag '{switchover_flag}'. Actual health check result was {'HEALTHY' if actual_health_status == 1 else 'UNHEALTHY'}. No failover will occur."
        elif switchover_flag == 'force_unhealthy':
            final_published_metric_value = 0
            overall_status = "UNHEALTHY"
            status_code = 500
            logger.info(f"Health forced to UNHEALTHY by SSM flag '{switchover_flag}'. Actual health was {actual_health_status}.")
            notification_subject = "Health Check Alert: CRITICAL - Status Forced Unhealthy"
            notification_message = f"Health check status is manually forced to UNHEALTHY via SSM flag '{switchover_flag}'. Actual health check result was {'HEALTHY' if actual_health_status == 1 else 'UNHEALTHY'}. Failover may be triggered."
        elif switchover_flag == 'auto':
            final_published_metric_value = actual_health_status
            overall_status = "HEALTHY" if actual_health_status == 1 else "UNHEALTHY"
            status_code = 200 if actual_health_status == 1 else 500
            logger.info(f"Health is in 'auto' mode. Publishing actual health: {final_published_metric_value}.")
            if actual_health_status == 0:
                notification_subject = "Health Check Alert: CRITICAL - Automated Health Check Failed"
                notification_message = f"Automated health check failed. Overall status: UNHEALTHY. Failover may be triggered.\n\nFailed Services:\n" + "\n".join(failed_services)
            else:
                notification_subject = "Health Check Info: Automated Health Check Passed"
                notification_message = "Automated health check passed. Overall status: HEALTHY. No failover triggered."
        else:
            logger.error(f"Invalid switchover_flag value: '{switchover_flag}'. Expected 'auto', 'force_healthy', or 'force_unhealthy'. Defaulting to UNHEALTHY for published metric.")
            final_published_metric_value = 0 # Invalid flag means unhealthy
            overall_status = "UNHEALTHY"
            status_code = 500
            notification_subject = "Health Check Alert: CRITICAL - Invalid Switchover Flag"
            notification_message = f"The SSM parameter '{SWITCHOVER_FLAG_SSM_PATH}' has an invalid value: '{switchover_flag}'. Expected 'auto', 'force_healthy', or 'force_unhealthy'. Health check defaulting to UNHEALTHY."

    except ClientError as e:
        logger.error(f"AWS Client Error during main execution (SSM or CloudWatch): {e}")
        final_published_metric_value = 0 # On AWS API error, default to unhealthy
        overall_status = "UNHEALTHY"
        status_code = 500
        notification_subject = "Health Check Alert: CRITICAL - AWS API Error"
        notification_message = f"The health check Lambda encountered an AWS API error: {e}. Health check defaulting to UNHEALTHY."
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing JSON from SSM parameter: {e}. Ensure SERVICE_HEALTH_ENDPOINTS_SSM_PATH contains valid JSON.")
        final_published_metric_value = 0
        overall_status = "UNHEALTHY"
        status_code = 500
        notification_subject = "Health Check Alert: CRITICAL - Invalid JSON in SSM"
        notification_message = f"The SSM parameter '{SERVICE_HEALTH_ENDPOINTS_SSM_PATH}' contains invalid JSON: {e}. Health check defaulting to UNHEALTHY."
    except Exception as e:
        logger.error(f"An unexpected error occurred during main execution: {e}", exc_info=True)
        final_published_metric_value = 0
        overall_status = "UNHEALTHY"
        status_code = 500
        notification_subject = "Health Check Alert: CRITICAL - Unexpected Error"
        notification_message = f"The health check Lambda encountered an unexpected error: {e}. Health check defaulting to UNHEALTHY."
    
    # Always publish the BinaryHealthCheck metric with the determined value
    publish_cloudwatch_metric(
        CLOUDWATCH_NAMESPACE,
        CLOUDWATCH_METRIC_NAME,
        final_published_metric_value,
        CLOUDWATCH_METRIC_UNIT,
        dimensions
    )

    # Send SNS notification based on the determined status and context
    send_sns_notification(notification_subject, notification_message)

    return {
        'statusCode': status_code,
        'body': json.dumps({
            'message': f"Service health check completed. Overall status: {overall_status}.",
            'switchover_flag_mode': switchover_flag,
            'actual_health_check_result': actual_health_status,
            'published_binary_health_value': final_published_metric_value,
            'published_cloudwatch_namespace': CLOUDWATCH_NAMESPACE,
            'notification_sent': notification_subject # Indicate notification attempt
        })
    }
