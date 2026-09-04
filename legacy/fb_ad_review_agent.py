import random

class ReviewAgent:
    def review(self, ad_copy):
        improvements = []

        # Facebook Ad Guidelines - Avoid Unrealistic Claims
        if any(term in ad_copy.lower() for term in ["guaranteed", "100%", "instant results", "risk-free"]):
            improvements.append("⚠️ Avoid using 'guaranteed', '100%', 'instant results', or 'risk-free' as Facebook may flag unrealistic claims.")
        
        # Emotional Triggers - Enhance Emotional Pull
        if "couldn’t believe" in ad_copy:
            improvements.append("🔥 Amplify emotions with success metrics or testimonials – avoid vague phrases.")

        # Personalization and Compliance
        if "you" not in ad_copy.lower():
            improvements.append("💡 Use 'you' naturally to personalize the ad. Avoid forcing it, as overuse may reduce credibility.")
        
        # CTA Optimization - Facebook Best Practice
        if "Click below" in ad_copy:
            if "ebook" in ad_copy.lower() or "course" in ad_copy.lower():
                improvements.append("🚀 Use 'Sign Up' or 'Download Now' for digital products to align with best practices.")
            elif "gadget" in ad_copy.lower() or "item" in ad_copy.lower():
                improvements.append("🚀 Use 'Order Now' or 'Buy Today' for physical products to increase urgency and relevance.")
            else:
                improvements.append("🚀 Use a more action-oriented CTA like 'Get Started Now' or 'Learn More' to align with Facebook best practices.")
        
        # Avoid Overuse of Emojis (Facebook Flags Excessive Emojis)
        paragraphs = ad_copy.split("\n\n")
        for i, para in enumerate(paragraphs):
            if para.count("🔥") > 1 or para.count("✅") > 1:
                improvements.append(f"⚠️ Limit emoji use in paragraph {i+1} – Facebook may reduce reach for ads with too many emojis.")
        
        # Clear Value Proposition in First Line
        if not any(word in ad_copy.lower() for word in ["save", "transform", "unlock", "discover", "achieve", "boost", "enhance"]):
            improvements.append("💡 Ensure the first line highlights a clear benefit or transformation.")
        
        # Original and Improved Ad Feedback
        if improvements:
            return f"Original Ad:\n{ad_copy}\n\nSuggested Improvements:\n" + "\n".join(improvements)
        else:
            return f"Ad is strong and ready to go (Facebook compliant):\n\n{ad_copy}"
