class ReviewAgent:
    """
    Reviews ad copy against basic Facebook guidelines and best practices.
    Suggests improvements if needed.
    """

    def review(self, ad_copy, ad_performance=None):
        improvements = []

        # Avoid Unrealistic Claims
        if any(term in ad_copy.lower() for term in ["guaranteed", "100%", "instant results", "risk-free"]):
            improvements.append("⚠️ Avoid using 'guaranteed', '100%', 'instant results', or 'risk-free' as Facebook may flag unrealistic claims.")
        
        # Enhance Emotional Pull
        if "couldn’t believe" in ad_copy.lower():
            improvements.append("🔥 Amplify emotions with success metrics or testimonials – avoid vague phrases.")

        # Check Personalization
        if "you" not in ad_copy.lower():
            improvements.append("💡 Use 'you' naturally to personalize the ad. Avoid forcing it, as overuse may reduce credibility.")

        # CTA Optimization
        if "click below" in ad_copy.lower():
            if "ebook" in ad_copy.lower() or "course" in ad_copy.lower():
                improvements.append("🚀 Use 'Sign Up' or 'Download Now' for digital products to align with best practices.")
            elif "gadget" in ad_copy.lower() or "item" in ad_copy.lower():
                improvements.append("🚀 Use 'Order Now' or 'Buy Today' for physical products to increase urgency and relevance.")
            else:
                improvements.append("🚀 Use action-oriented CTAs like 'Get Started Now' or 'Learn More' to align with Facebook best practices.")

        # Check Emoji Overuse
        paragraphs = ad_copy.split("\n\n")
        for i, para in enumerate(paragraphs):
            if len(para) > 50 and (para.count("🔥") > 1 or para.count("✅") > 1):
                improvements.append(f"⚠️ Limit emoji use in paragraph {i+1} – Facebook may reduce reach for ads with too many emojis.")

        # Check Value Proposition
        if not any(word in ad_copy.lower() for word in ["save", "transform", "unlock", "discover", "achieve", "boost", "enhance"]):
            improvements.append("💡 Ensure the first line highlights a clear benefit or transformation.")

        # Performance-Based Adjustments
        # If you have performance data (e.g., CTR < 1.5%), we can suggest improvements:
        if ad_performance and "ctr" in ad_performance:
            if ad_performance["ctr"] < 1.5:
                improvements.append("📉 Consider reworking the hook for higher engagement. CTR is below 1.5%.")

        # Final Output
        if improvements:
            return (
                f"Original Ad:\n{ad_copy}\n\n"
                "Suggested Improvements:\n" + "\n".join(improvements)
            )
        else:
            return f"Ad is strong and ready to go (Facebook compliant):\n\n{ad_copy}"


if __name__ == "__main__":
    # Quick test
    agent = ReviewAgent()
    sample_ad = "Click below for instant results! Couldn’t believe how well it worked!"
    feedback = agent.review(sample_ad, ad_performance={"ctr": 1.2})
    print(feedback)
