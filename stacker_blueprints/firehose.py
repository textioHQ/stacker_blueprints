from awacs.aws import (
    Action,
    Allow,
    Bool,
    Condition,
    Policy,
    Principal,
    AWSPrincipal,
    Statement,
    StringEquals,
)
import awacs.logs
import awacs.s3
import awacs.kms
import awacs.firehose
from awacs import sts
from stacker.blueprints.base import Blueprint
from troposphere import (
    iam,
    kms,
    s3,
    GetAtt,
    Join,
    Output,
    Ref,
)


BUCKET = 'S3Bucket'
IAM_ROLE = 'IAMRole'
ROLE_POLICY = 'RolePolicy'
FIREHOSE_WRITE_POLICY = 'FirehoseWriteAccess'
LOGS_POLICY = 'LogsPolicy'
S3_WRITE_POLICY = 'S3WriteAccess'
LOGS_WRITE_POLICY = 'LogsWriteAccess'
KMS_KEY = "EncryptionKey"
KEY_ALIAS = "KeyAlias"


class FirehoseAction(Action):
    def __init__(self, action=None):
        self.prefix = "firehose"
        self.action = action


def s3_arn(bucket):
    return Join('', ['arn:aws:s3:::', bucket])


def logs_policy():
    statements = [
        Statement(
            Effect=Allow,
            Action=[
                awacs.logs.CreateLogStream,
                awacs.logs.CreateLogGroup,
            ],
            Resource=['*'],
        ),
    ]
    return Policy(Statement=statements)


def firehose_write_policy():
    statements = [
        Statement(
            Effect=Allow,
            Action=[
                awacs.firehose.CreateDeliveryStream,
                awacs.firehose.DeleteDeliveryStream,
                awacs.firehose.DescribeDeliveryStream,
                awacs.firehose.PutRecord,
                awacs.firehose.PutRecordBatch,
            ],
            Resource=['*'],
        ),
    ]
    return Policy(Statement=statements)


def logs_write_policy():
    statements = [
        Statement(
            Effect=Allow,
            Action=[
                awacs.logs.PutLogEvents,
            ],
            Resource=['*'],
        ),
    ]
    return Policy(Statement=statements)


def s3_write_policy(bucket):
    statements = [
        Statement(
            Effect=Allow,
            Action=[
                awacs.s3.AbortMultipartUpload,
                awacs.s3.GetBucketLocation,
                awacs.s3.GetObject,
                awacs.s3.ListBucket,
                awacs.s3.ListBucketMultipartUploads,
                awacs.s3.PutObject,
            ],
            Resource=[
                s3_arn(bucket),
                s3_arn(Join("/", [bucket, "*"]))
            ],
        ),
    ]
    return Policy(Statement=statements)


def kms_key_policy(key_use_arns, key_admin_arns):
    """ Creates a key policy for use of a KMS Key.

    key_use_arns is a list of arns that should have access to use the KMS
    key.
    """

    root_arn = Join(":", ["arn:aws:iam:", Ref("AWS::AccountId"), "root"])

    statements = []
    statements.append(
        Statement(
            Sid="Enable IAM User Permissions",
            Effect=Allow,
            Principal=AWSPrincipal(root_arn),
            Action=[
                Action("kms", "*"),
            ],
            Resource=["*"]
        )
    )
    if key_use_arns:
        statements.append(
            Statement(
                Sid="Allow use of the key",
                Effect=Allow,
                Principal=AWSPrincipal(key_use_arns),
                Action=[
                    awacs.kms.Encrypt,
                    awacs.kms.Decrypt,
                    awacs.kms.ReEncrypt,
                    awacs.kms.GenerateDataKey,
                    awacs.kms.GenerateDataKeyWithoutPlaintext,
                    awacs.kms.DescribeKey,
                ],
                Resource=["*"]
            )
        )

        statements.append(
            Statement(
                Sid="Allow attachment of persistent resources",
                Effect=Allow,
                Principal=AWSPrincipal(key_use_arns),
                Action=[
                    awacs.kms.CreateGrant,
                    awacs.kms.ListGrants,
                    awacs.kms.RevokeGrant,
                ],
                Resource=["*"],
                Condition=Condition(Bool("kms:GrantIsForAWSResource", True))
            )
        )

    if key_admin_arns:
        statements.append(
            Statement(
                Sid="Allow access for Key Administrators",
                Effect=Allow,
                Principal=AWSPrincipal(key_admin_arns),
                Action=[
                    Action("kms", "Create*"),
                    Action("kms", "Describe*"),
                    Action("kms", "Enable*"),
                    Action("kms", "List*"),
                    Action("kms", "Put*"),
                    Action("kms", "Update*"),
                    Action("kms", "Revoke*"),
                    Action("kms", "Disable*"),
                    Action("kms", "Get*"),
                    Action("kms", "Delete*"),
                    Action("kms", "ScheduleKeyDeletion"),
                    Action("kms", "CancelKeyDeletion"),
                ],
                Resource=["*"],
            )
        )

    return Policy(Version="2012-10-17", Id="key-default-1",
                  Statement=statements)


class Firehose(Blueprint):
    VARIABLES = {
        "RoleNames": {
            "type": list,
            "description": "A list of role names that should have access to "
                           "write to the firehose stream.",
            "default": [],
        },
        "GroupNames": {
            "type": list,
            "description": "A list of group names that should have access to "
                           "write to the firehose stream.",
            "default": [],
        },
        "UserNames": {
            "type": list,
            "description": "A list of user names that should have access to "
                           "write to the firehose stream.",
            "default": [],
        },
        "BucketName": {
            "type": str,
            "description": "Name for the S3 Bucket",
            "default": "",
        },
        "EncryptS3Bucket": {
            "type": bool,
            "description": "If set to true, a KMS key will be created to use "
                           "for encrypting the S3 Bucket's contents. If set "
                           "to false, no encryption will occur. Default: true",
            "default": True,
        },
        "EnableKeyRotation": {
            "type": bool,
            "description": "Whether to enable key rotation on the KMS key "
                           "generated if EncryptS3Bucket is set to true. "
                           "Default: true",
            "default": True,
        },
        "KeyUseArns": {
            "type": list,
            "description": "A list of ARNs that need access to the KMS key "
                           "created with EncryptS3Bucket",
            "default": [],
        },
        "KeyAdminArns": {
            "type": list,
            "description": "A list of ARNs that need to admin the KMS key "
                           "created with EncryptS3Bucket",
            "default": [],
        }
    }

    def create_kms_key(self):
        t = self.template
        variables = self.get_variables()

        if not variables["EncryptS3Bucket"]:
            return

        key_description = Join(
            "",
            [
                "S3 Bucket kms encryption key for stack ",
                Ref("AWS::StackName")
            ]
        )

        key_use_arns = variables["KeyUseArns"]
        # auto add the created IAM Role
        key_use_arns.append(GetAtt(IAM_ROLE, "Arn"))

        key_admin_arns = variables["KeyAdminArns"]

        t.add_resource(
            kms.Key(
                KMS_KEY,
                Description=key_description,
                Enabled=True,
                EnableKeyRotation=variables["EnableKeyRotation"],
                KeyPolicy=kms_key_policy(key_use_arns, key_admin_arns),
            )
        )

        t.add_resource(
            kms.Alias(
                KEY_ALIAS,
                AliasName="alias/%s" % self.context.get_fqn(self.name),
                TargetKeyId=Ref(KMS_KEY)
            )
        )

        key_arn = Join(
            "",
            [
                "arn:aws:kms:",
                Ref("AWS::Region"),
                ":",
                Ref("AWS::AccountId"),
                ":key/",
                Ref(KMS_KEY)
            ]
        )
        t.add_output(Output("KmsKeyArn", Value=key_arn))
        t.add_output(Output("KmsKeyId", Value=Ref(KMS_KEY)))
        t.add_output(Output("KmsKeyAlias", Value=Ref(KEY_ALIAS)))

    def create_bucket(self):
        t = self.template
        variables = self.get_variables()

        bucket_name = variables.get("BucketName") or Ref("AWS::NoValue")

        t.add_resource(
            s3.Bucket(
                BUCKET,
                BucketName=bucket_name,
            )
        )
        t.add_output(Output('Bucket', Value=Ref(BUCKET)))

    def generate_iam_policies(self):
        name_prefix = self.context.get_fqn(self.name)
        s3_policy = iam.Policy(
            S3_WRITE_POLICY,
            PolicyName='{}-s3-write'.format(name_prefix),
            PolicyDocument=s3_write_policy(Ref(BUCKET)),
        )
        logs_policy = iam.Policy(
            LOGS_WRITE_POLICY,
            PolicyName='{}-logs-write'.format(name_prefix),
            PolicyDocument=logs_write_policy(),
        )
        return [s3_policy, logs_policy]

    def create_role(self):
        t = self.template

        statements = [
            Statement(
                Principal=Principal('Service', ['firehose.amazonaws.com']),
                Effect=Allow,
                Action=[sts.AssumeRole],
                Condition=Condition(
                    StringEquals('sts:ExternalId', Ref('AWS::AccountId')),
                ),
            ),
        ]
        firehose_role_policy = Policy(Statement=statements)
        t.add_resource(
            iam.Role(
                IAM_ROLE,
                AssumeRolePolicyDocument=firehose_role_policy,
                Path='/',
                Policies=self.generate_iam_policies(),
            ),
        )
        t.add_output(Output('Role', Value=Ref(IAM_ROLE)))
        t.add_output(Output('RoleArn', Value=GetAtt(IAM_ROLE, 'Arn')))

    def create_policy(self):
        name_prefix = self.context.get_fqn(self.name)
        t = self.template
        variables = self.get_variables()

        external_roles = variables.get("RoleNames") or Ref("AWS::NoValue")
        external_groups = variables.get("GroupNames") or Ref("AWS::NoValue")
        external_users = variables.get("UserNames") or Ref("AWS::NoValue")

        create_policy = any([
            variables["RoleNames"],
            variables["GroupNames"],
            variables["UserNames"],
        ])

        if create_policy:
            t.add_resource(
                iam.PolicyType(
                    FIREHOSE_WRITE_POLICY,
                    PolicyName='{}-firehose'.format(name_prefix),
                    PolicyDocument=firehose_write_policy(),
                    Roles=external_roles,
                    Groups=external_groups,
                    Users=external_users,
                ),
            )
            t.add_resource(
                iam.PolicyType(
                    LOGS_POLICY,
                    PolicyName='{}-logs'.format(name_prefix),
                    PolicyDocument=logs_policy(),
                    Roles=external_roles,
                    Groups=external_groups,
                    Users=external_users,
                ),
            )

    def create_template(self):
        self.create_kms_key()
        self.create_policy()
        self.create_bucket()
        self.create_role()
