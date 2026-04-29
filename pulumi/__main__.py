
"""Mikshi-VLM EKS infrastructure: ECR repos, IAM roles for IRSA, GitHub OIDC."""

import json
from pathlib import Path

import pulumi
import pulumi_aws as aws

cfg = pulumi.Config()
ACCOUNT_ID = cfg.require("account_id")
CLUSTER_NAME = cfg.require("cluster_name")
OIDC_PROVIDER = cfg.require("oidc_provider")
GITHUB_ORG = cfg.require("github_org")
GITHUB_REPOS = cfg.get_object("github_repos") or [cfg.require("github_repo")]

REGION = aws.config.region
NAMESPACE = "mikshi-vlm"
OIDC_ARN = f"arn:aws:iam::{ACCOUNT_ID}:oidc-provider/{OIDC_PROVIDER}"

SERVICES = json.loads((Path(__file__).parent.parent / "services.json").read_text())["services"]


def stmt_secrets():
    return [
        {
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
            "Resource": [f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:Mikshi-VLM/*"],
        },
        {
            "Effect": "Allow",
            "Action": ["kms:Decrypt"],
            "Resource": "*",
            "Condition": {"StringEquals": {"kms:ViaService": f"secretsmanager.{REGION}.amazonaws.com"}},
        },
    ]


def stmt_s3():
    bucket = f"mikshi-vlm-media-prod-{ACCOUNT_ID}"
    return [{
        "Effect": "Allow",
        "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
        "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
    }]


def stmt_msk():
    return [{
        "Effect": "Allow",
        "Action": [
            "kafka-cluster:Connect", "kafka-cluster:DescribeCluster",
            "kafka-cluster:DescribeTopic", "kafka-cluster:CreateTopic",
            "kafka-cluster:WriteData", "kafka-cluster:WriteDataIdempotently",
            "kafka-cluster:ReadData", "kafka-cluster:DescribeGroup",
            "kafka-cluster:AlterGroup",
        ],
        "Resource": "*",
    }]


IRSA_BUILDERS = {"secrets": stmt_secrets, "s3": stmt_s3, "msk": stmt_msk}


def irsa_trust(service_account):
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Federated": OIDC_ARN},
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {
                "StringEquals": {
                    f"{OIDC_PROVIDER}:sub": f"system:serviceaccount:{NAMESPACE}:{service_account}",
                    f"{OIDC_PROVIDER}:aud": "sts.amazonaws.com",
                }
            },
        }],
    })


service_role_arns = {}
for svc in SERVICES:
    name = svc["name"]
    sa = svc.get("deployment", name)

    aws.ecr.Repository(
        f"ecr-{name}",
        name=f"mikshi-vlm/{name}",
        image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(scan_on_push=True),
        force_delete=False,
        tags={"Project": "Mikshi-VLM", "Service": name},
    )

    tokens = svc.get("irsa", [])
    if not tokens:
        continue

    statements = []
    for t in tokens:
        if t not in IRSA_BUILDERS:
            raise ValueError(f"Unknown IRSA token '{t}' for service '{name}'")
        statements.extend(IRSA_BUILDERS[t]())

    role = aws.iam.Role(
        f"role-{name}",
        name=f"Mikshi-VLM-{name}-role",
        assume_role_policy=irsa_trust(sa),
        tags={"Project": "Mikshi-VLM", "Service": name},
    )
    aws.iam.RolePolicy(
        f"role-{name}-inline",
        role=role.name,
        policy=json.dumps({"Version": "2012-10-17", "Statement": statements}),
    )
    service_role_arns[name] = role.arn


github_oidc = aws.iam.OpenIdConnectProvider(
    "github-oidc",
    url="https://token.actions.githubusercontent.com",
    client_id_lists=["sts.amazonaws.com"],
    thumbprint_lists=["6938fd4d98bab03faadb97b34396831e3780aea1"],
)

cicd_trust = github_oidc.arn.apply(lambda arn: json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Federated": arn},
        "Action": "sts:AssumeRoleWithWebIdentity",
        "Condition": {
            "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
            "StringLike": {
                "token.actions.githubusercontent.com:sub": [
                    f"repo:{GITHUB_ORG}/{r}:*" for r in GITHUB_REPOS
                ],
            },
        },
    }],
}))

cicd_role = aws.iam.Role(
    "role-cicd",
    name="Mikshi-VLM-cicd-role",
    assume_role_policy=cicd_trust,
    tags={"Project": "Mikshi-VLM", "Service": "cicd"},
)

aws.iam.RolePolicy(
    "role-cicd-inline",
    role=cicd_role.name,
    policy=json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage",
                    "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload", "ecr:PutImage",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": ["eks:DescribeCluster", "eks:ListClusters"],
                "Resource": f"arn:aws:eks:{REGION}:{ACCOUNT_ID}:cluster/{CLUSTER_NAME}",
            },
        ],
    }),
)

for svc_name, arn in service_role_arns.items():
    pulumi.export(f"{svc_name.replace('-', '_')}_role_arn", arn)
pulumi.export("cicd_role_arn", cicd_role.arn)
