import os
import json
import logging
import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

# Initialize AWS SDK clients
elbv2_client = boto3.client('elbv2')
cloudwatch_client = boto3.client('cloudwatch')

async def get_load_balancer_arn(load_balancer_name):
    """
    Retrieves the ARN of an ALB given its name.
    """
    try:
        response = elbv2_client.describe_load_balancers(Names=[load_balancer_name])
        if response['LoadBalancers']:
            return response['LoadBalancers'][0]['LoadBalancerArn']
        else:
            logger.warning(f"Load balancer '{load_balancer_name}' not found.")
            return None
    except ClientError as e:
        logger.error(f"Error describing load balancer '{load_balancer_name}': {e}")
        raise

async def get_target_group_arns_from_alb(load_balancer_name):
    """
    Retrieves all target group ARNs associated with a given ALB.
    """
    target_group_arns = []
    alb_arn = await get_load_balancer_arn(load_balancer_name)

    if not alb_arn:
        return []

    try:
        # Get listeners for the ALB
        listeners_paginator = elbv2_client.get_paginator('describe_listeners')
        listener_pages = listeners_paginator.paginate(LoadBalancerArn=alb_arn)

        for page in listener_pages:
            for listener in page['Listeners']:
                # Get rules for each listener
                rules_paginator = elbv2_client.get_paginator('describe_rules')
                rule_pages = rules_paginator.paginate(ListenerArn=listener['ListenerArn'])

                for rule_page in rule_pages:
                    for rule in rule_page['Rules']:
                        for action in rule['Actions']:
                            if action['Type'] == 'forward' and 'TargetGroupArn' in action['ForwardConfig']['TargetGroups'][0]:
                                # Extract all target groups from the forward action
                                for tg in action['ForwardConfig']['TargetGroups']:
                                    target_group_arns.append(tg['TargetGroupArn'])

        # Remove duplicates, as a target group can be used in multiple rules/listeners
        return list(set(target_group_arns))

    except ClientError as e:
        logger.error(f"Error getting target groups for ALB '{load_balancer_name}': {e}")
        raise

async def check_target_group_health(target_group_arn):
    """
    Checks the health status of targets within a single target group.
    Returns a boolean indicating if the target group is considered healthy
    (at least one healthy target), and details of its targets.
    """
    try:
        response = elbv2_client.describe_target_health(TargetGroupArn=target_group_arn)
        targets = response['TargetHealthDescriptions']

        is_healthy = False
        healthy_targets = []
        unhealthy_targets = []

        for target in targets:
            status = target['TargetHealth']['State']
            target_id = target['Target']['Id'] # This is the Fargate task IP or instance ID
            target_port = target['Target']['Port']

            if status == 'healthy':
                is_healthy = True # If at least one target is healthy, the TG is considered healthy
                healthy_targets.append({'Id': target_id, 'Port': target_port, 'Status': status})
            else:
                unhealthy_targets.append({'Id': target_id, 'Port': target_port, 'Status': status, 'Reason': target['TargetHealth'].get('Reason', 'N/A')})

        # Log detailed status for debugging
        logger.debug(f"Target Group '{target_group_arn}' health check: {'Healthy' if is_healthy else 'Unhealthy'}")
        logger.debug(f"  Healthy targets: {healthy_targets}")
        logger.debug(f"  Unhealthy targets: {unhealthy_targets}")

        return {
            'isHealthy': is_healthy,
            'targets': {
                'healthy': healthy_targets,
                'unhealthy': unhealthy_targets
            }
        }
    except ClientError as e:
        logger.error(f"Error checking health for target group '{target_group_arn}': {e}")
        raise

async def publish_metric(metric_name, value, namespace, unit):
    """
    Publishes a custom metric to CloudWatch.
    """
    try:
        cloudwatch_client.put_metric_data(
            Namespace=namespace,
            MetricData=[
                {
                    'MetricName': metric_name,
                    'Value': value,
                    'Unit': unit
                },
            ]
        )
        logger.info(f"Published metric '{metric_name}' with value {value} to namespace '{namespace}'")
    except ClientError as e:
        logger.error(f"Error publishing metric '{metric_name}': {e}")
        # Don't re-raise, as metric publishing should ideally not fail the health check itself

async def handler(event, context):
    """
    Lambda function to check the health status of ALB target groups
    and publish custom CloudWatch metrics for Route 53 health checks.
    """
    load_balancer_name = os.environ.get('LOAD_BALANCER_NAME')
    health_threshold_percentage = float(os.environ.get('HEALTH_THRESHOLD_PERCENTAGE', '80'))
    health_check_namespace = os.environ.get('HEALTH_CHECK_NAMESPACE', 'MyApp/HealthChecks')

    # Validate environment variables
    if not load_balancer_name:
        logger.error("LOAD_BALANCER_NAME environment variable is not set.")
        await publish_metric("OverallApplicationHealthPercentage", 0, health_check_namespace, 'Percent')
        await publish_metric("BinaryApplicationHealthStatus", 0, health_check_namespace, 'Count')
        return { 'statusCode': 500, 'body': json.dumps('Load Balancer name not configured.') }

    try:
        target_group_arns = await get_target_group_arns_from_alb(load_balancer_name)

        total_target_groups_found = len(target_group_arns)
        if total_target_groups_found == 0:
            logger.warn(f"No target groups found for ALB '{load_balancer_name}'. Overall health considered 0% for failover purposes.")
            overall_health_percentage = 0.0
            await publish_metric("OverallApplicationHealthPercentage", overall_health_percentage, health_check_namespace, 'Percent')
            await publish_metric("BinaryApplicationHealthStatus", 0, health_check_namespace, 'Count')
            return { 'statusCode': 200, 'body': json.dumps('Health check completed. No target groups found.') }

        healthy_target_groups_count = 0
        all_target_group_statuses = {}

        for arn in target_group_arns:
            health_status = await check_target_group_health(arn) # Call async function
            all_target_group_statuses[arn] = health_status
            if health_status['isHealthy']:
                healthy_target_groups_count += 1

        overall_health_percentage = (healthy_target_groups_count / total_target_groups_found) * 100.0

        logger.info(f"Overall application health: {overall_health_percentage:.2f}% ({healthy_target_groups_count}/{total_target_groups_found} healthy target groups).")
        logger.debug(f"Detailed target group statuses: {json.dumps(all_target_group_statuses, indent=2)}")

        await publish_metric("OverallApplicationHealthPercentage", overall_health_percentage, health_check_namespace, 'Percent')
        binary_health_status = 1 if overall_health_percentage >= health_threshold_percentage else 0
        await publish_metric("BinaryApplicationHealthStatus", binary_health_status, health_check_namespace, 'Count')

        # The Lambda's return statusCode determines the health for Route 53 (if it's a direct endpoint health check)
        # Or, more commonly with CloudWatch metrics, a 200 is always returned, and Route 53 monitors the metric.
        # Given your Node.js original, returning 200 is fine, and relying on CloudWatch Metric for Route 53.
        return {
            'statusCode': 200,
            'body': json.dumps(f'Health check completed. Overall health: {overall_health_percentage:.2f}%')
        }

    except Exception as e:
        logger.error(f"An unhandled error occurred in Lambda handler: {e}", exc_info=True) # exc_info to print traceback
        await publish_metric("OverallApplicationHealthPercentage", 0, health_check_namespace, 'Percent')
        await publish_metric("BinaryApplicationHealthStatus", 0, health_check_namespace, 'Count')
        return { 'statusCode': 500, 'body': json.dumps(f'Lambda execution failed: {str(e)}') }
